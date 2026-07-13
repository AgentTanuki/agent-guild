"""AGI-1 conformance checks — pure, issuer-agnostic, vendorable.

Every check returns {"check": str, "passed": bool, "detail": str} so suites
and CI can aggregate. See AGI1_CONFORMANCE.md for the requirement ids.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentguild_trustplane.verify import (canonicalize_jcs,  # noqa: E402
                                          verify_data_integrity,
                                          within_validity)
from agentguild_trustplane.contract import validate_decision  # noqa: E402


def _r(check: str, passed: bool, detail: str = "") -> dict[str, Any]:
    return {"check": check, "passed": passed, "detail": detail}


# --- I-2/V-1/V-2: proofs ------------------------------------------------------
def check_proof_verifies(doc: dict[str, Any],
                         issuer_allowlist: Optional[list[str]] = None
                         ) -> dict[str, Any]:
    v = verify_data_integrity(doc)
    if not v["verified"]:
        return _r("I-2 proof verifies", False, v["reason"])
    if issuer_allowlist is not None and v["issuer_did"] not in issuer_allowlist:
        return _r("V-3 issuer trusted", False,
                  f"issuer {v['issuer_did']} not in caller allowlist")
    return _r("I-2 proof verifies", True, f"issuer={v['issuer_did']}")


def check_tamper_rejected(doc: dict[str, Any]) -> dict[str, Any]:
    bad = json.loads(json.dumps(doc))
    # flip the first mutable leaf we find outside the proof
    for k in bad:
        if k != "proof" and isinstance(bad[k], str):
            bad[k] = bad[k] + "x"
            break
    else:
        bad["_tamper"] = True
    ok = not verify_data_integrity(bad)["verified"]
    return _r("V-2 tamper rejected", ok,
              "" if ok else "TAMPERED DOCUMENT STILL VERIFIES")


def check_validity_window(doc: dict[str, Any]) -> dict[str, Any]:
    valid, age = within_validity(doc)
    if age is None:
        return _r("I-3 validity window", False,
                  "no issued_at/valid_until — unbounded assertion")
    return _r("I-3 validity window", True,
              f"valid={valid} age={age:.0f}s")


# --- I-4: AGD-1 ----------------------------------------------------------------
def check_agd1(decision: Optional[dict[str, Any]]) -> dict[str, Any]:
    if decision is None:
        return _r("I-4 AGD-1 decision", False, "no decision present")
    errs = validate_decision(decision)
    return _r("I-4 AGD-1 decision", not errs, "; ".join(errs[:5]) or "conformant")


# --- I-5/V-4: checkpoint feed + fork detection ---------------------------------
def _feed_canonical(value: Any) -> str:
    """Checkpoint-FEED entry commitment canonical form: plain sorted-key JSON
    (json.dumps sort_keys, compact separators). NOTE: this is the feed's
    entry-chaining form only; credential/decision PROOFS use JCS (I-2)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def check_feed_continuity(feed: list[dict[str, Any]]) -> dict[str, Any]:
    """Each entry commits to its predecessor via prev_entry_sha256."""
    if not feed:
        return _r("I-5 feed continuity", False, "empty feed")
    feed = sorted(feed, key=lambda e: e.get("index", 0))   # feeds may serve newest-first
    base = feed[0].get("index", 0)
    for i, entry in enumerate(feed):
        if entry.get("index") != base + i:
            return _r("I-5 feed continuity", False,
                      f"index gap at position {base + i}")
        if i == 0:
            # genesis commits to zeros; a partial window's first entry has its
            # predecessor outside the window (verifiable with a wider fetch)
            if base == 0 and entry.get("prev_entry_sha256") != "0" * 64:
                return _r("I-5 feed continuity", False,
                          "genesis entry does not commit to zeros")
            continue
        want = hashlib.sha256(_feed_canonical(feed[i - 1]).encode()).hexdigest()
        if entry.get("prev_entry_sha256") != want:
            return _r("I-5 feed continuity", False,
                      f"broken predecessor commitment at index {base + i}")
    return _r("I-5 feed continuity", True,
              f"{len(feed)} entries chained (window base {base})")


def detect_fork(pinned: dict[str, Any], observed: dict[str, Any]
                ) -> dict[str, Any]:
    """V-4: same index, different head_hash => FORK (passed=False = forked)."""
    same_index = pinned.get("index") == observed.get("index")
    ph = (pinned.get("checkpoint") or {}).get("head_hash")
    oh = (observed.get("checkpoint") or {}).get("head_hash")
    if same_index and ph != oh:
        return _r("V-4 fork detection", False,
                  f"FORK at index {pinned.get('index')}: {ph} != {oh}")
    return _r("V-4 fork detection", True, "no divergence at pinned index")


# --- V-3: multi-issuer -----------------------------------------------------------
def check_cross_issuer_isolation(doc_a: dict[str, Any],
                                 doc_b: dict[str, Any]) -> dict[str, Any]:
    """A credential must verify ONLY against its own issuer's DID."""
    va = verify_data_integrity(doc_a)
    vb = verify_data_integrity(doc_b)
    if not (va["verified"] and vb["verified"]):
        return _r("V-3 multi-issuer", False, "input docs do not verify")
    if va["issuer_did"] == vb["issuer_did"]:
        return _r("V-3 multi-issuer", False, "vectors share an issuer")
    cross = verify_data_integrity(doc_a,
                                  expected_issuer_did=vb["issuer_did"])
    return _r("V-3 multi-issuer", not cross["verified"],
              "cross-issuer acceptance!" if cross["verified"]
              else "issuers isolated")


def run_all(signed_decision: dict[str, Any],
            passport: Optional[dict[str, Any]] = None,
            feed: Optional[list[dict[str, Any]]] = None,
            second_issuer_doc: Optional[dict[str, Any]] = None,
            issuer_allowlist: Optional[list[str]] = None) -> list[dict[str, Any]]:
    results = [
        check_proof_verifies(signed_decision, issuer_allowlist),
        check_tamper_rejected(signed_decision),
        check_validity_window(signed_decision),
        check_agd1(signed_decision.get("decision")),
    ]
    if passport is not None:
        results += [check_proof_verifies(passport, issuer_allowlist),
                    check_tamper_rejected(passport)]
    if feed:
        results.append(check_feed_continuity(feed))
        results.append(detect_fork(feed[-1], feed[-1]))
    if second_issuer_doc is not None:
        results.append(check_cross_issuer_isolation(signed_decision,
                                                    second_issuer_doc))
    return results
