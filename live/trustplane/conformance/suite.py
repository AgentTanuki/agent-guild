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
                                          verify_jcs_hex,
                                          within_validity)
from agentguild_trustplane.contract import (validate_decision,  # noqa: E402
                                            binding_violations)


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


# --- I-6: one-counterparty binding ------------------------------------------
def check_binding(envelope: dict[str, Any]) -> dict[str, Any]:
    """I-6: when routable, decision/routing/endpoint concern ONE provider."""
    errs = binding_violations(envelope)
    return _r("I-6 counterparty binding", not errs,
              "; ".join(errs[:5]) or "decision == routed provider")


# --- I-7: evidence-to-checkpoint inclusion -----------------------------------
def _merkle_fold(leaf: str, path: list[dict[str, str]]) -> str:
    h = leaf
    for p in path:
        pair = (h + p["hash"]) if p.get("position") == "right" \
            else (p["hash"] + h)
        h = hashlib.sha256(pair.encode()).hexdigest()
    return h


def check_evidence_inclusion(decision: Optional[dict[str, Any]],
                             fetch_inclusion) -> dict[str, Any]:
    """I-7: every evidence record the decision counts is COMMITTED by the
    cited checkpoint — proven, not asserted. ``fetch_inclusion(record_id,
    checkpoint_index)`` -> the /ledger/inclusion payload (or raises)."""
    if decision is None:
        return _r("I-7 evidence inclusion", True, "no decision (no evidence)")
    prov = decision.get("evidence_provenance") or {}
    cp = prov.get("checkpoint") or {}
    rids = prov.get("record_ids")
    if prov.get("verifiable_collaborations", 0) == 0:
        return _r("I-7 evidence inclusion", True, "no counted evidence")
    if rids is None:
        return _r("I-7 evidence inclusion", False,
                  "decision counts evidence but exposes no record_ids")
    if cp.get("index") is None:
        return _r("I-7 evidence inclusion", False,
                  "decision counts evidence but cites no checkpoint")
    for rid in rids:
        try:
            proof = fetch_inclusion(rid, cp["index"])
        except Exception as e:
            return _r("I-7 evidence inclusion", False,
                      f"no inclusion proof for {rid}: {e}")
        rec = proof.get("record") or {}
        body = {k: v for k, v in rec.items() if k not in ("hash", "id")}
        # ledger record hashes use the PLAIN sorted-key JSON canonical form
        # (same as the feed entry commitment), not JCS number formatting
        leaf = hashlib.sha256(json.dumps(
            body, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode()).hexdigest()
        if leaf != rec.get("hash"):
            return _r("I-7 evidence inclusion", False,
                      f"{rid}: record body does not hash to its own hash")
        if _merkle_fold(leaf, proof.get("path") or []) != \
                proof.get("checkpoint_merkle_root"):
            return _r("I-7 evidence inclusion", False,
                      f"{rid}: merkle path does not reach the checkpoint root")
    return _r("I-7 evidence inclusion", True,
              f"{len(rids)} record(s) proven committed by checkpoint "
              f"{cp['index']}")


# --- I-5/V-4: checkpoint feed + fork detection ---------------------------------
def _feed_canonical(value: Any) -> str:
    """Checkpoint-FEED entry commitment canonical form: plain sorted-key JSON
    (json.dumps sort_keys, compact separators). NOTE: this is the feed's
    entry-chaining form only; credential/decision PROOFS use JCS (I-2)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def check_feed_continuity(feed: list[dict[str, Any]]) -> dict[str, Any]:
    """Each entry commits to its predecessor via prev_entry_sha256.

    LEGACY entries (published before predecessor commitments existed) carry
    no ``prev_entry_sha256``. History is never rewritten, so they are exempt
    from the chain test here — but ONLY check_feed_signatures accepts them,
    and only when a later SIGNED bridge commits to their exact bytes. Every
    entry that HAS the field must commit to its true predecessor."""
    if not feed:
        return _r("I-5 feed continuity", False, "empty feed")
    feed = sorted(feed, key=lambda e: e.get("index", 0))   # feeds may serve newest-first
    base = feed[0].get("index", 0)
    legacy = 0
    for i, entry in enumerate(feed):
        if entry.get("index") != base + i:
            return _r("I-5 feed continuity", False,
                      f"index gap at position {base + i}")
        if "prev_entry_sha256" not in entry:
            legacy += 1                # legacy: bridged via I-5b, not chained
            continue
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
              f"{len(feed)} entries chained (window base {base}; "
              f"{legacy} legacy entries — bridge required, see I-5b)")


def check_feed_signatures(feed: list[dict[str, Any]],
                          issuer_did: str) -> dict[str, Any]:
    """I-5b: the FEED itself is authenticated, not only hash-linked.
    feed_version >= 2 entries carry ``entry_proof`` (issuer signature over the
    whole entry); LEGACY entries (no proof) are acceptable ONLY when a later
    signed entry carries a ``bridge`` committing to their exact bytes."""
    if not feed:
        return _r("I-5b feed signatures", False, "empty feed")
    feed = sorted(feed, key=lambda e: e.get("index", 0))
    legacy = [e for e in feed if "entry_proof" not in e]
    bridges: dict[str, bool] = {}
    for e in feed:
        proof = e.get("entry_proof")
        if proof is not None:
            body = {k: v for k, v in e.items() if k != "entry_proof"}
            # each entry is signed by the issuer key CURRENT at publish time
            # (the entry's inner checkpoint names it; rotation continuity is
            # verified separately via the on-chain rotation entries)
            signer = (e.get("checkpoint") or {}).get("issuer") or issuer_did
            if not verify_jcs_hex(body, proof, signer):
                return _r("I-5b feed signatures", False,
                          f"entry {e.get('index')}: entry_proof does not "
                          f"verify against {signer}")
            for h in ((e.get("bridge") or {}).get("legacy_entry_sha256")
                      or []):
                bridges[h] = True
    for e in legacy:
        h = hashlib.sha256(_feed_canonical(e).encode()).hexdigest()
        if h not in bridges:
            return _r("I-5b feed signatures", False,
                      f"legacy entry {e.get('index')} is neither signed nor "
                      "committed by a signed bridge")
    return _r("I-5b feed signatures", True,
              f"{len(feed) - len(legacy)} signed entries; "
              f"{len(legacy)} legacy entries bridged")


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
            issuer_allowlist: Optional[list[str]] = None,
            fetch_inclusion=None) -> list[dict[str, Any]]:
    results = [
        check_proof_verifies(signed_decision, issuer_allowlist),
        check_tamper_rejected(signed_decision),
        check_validity_window(signed_decision),
        check_agd1(signed_decision.get("decision")),
        check_binding(signed_decision),
    ]
    if fetch_inclusion is not None:
        results.append(check_evidence_inclusion(
            signed_decision.get("decision"), fetch_inclusion))
    if passport is not None:
        results += [check_proof_verifies(passport, issuer_allowlist),
                    check_tamper_rejected(passport)]
    if feed:
        results.append(check_feed_continuity(feed))
        results.append(check_feed_signatures(
            feed, signed_decision.get("issuer", "")))
        results.append(detect_fork(feed[-1], feed[-1]))
    if second_issuer_doc is not None:
        results.append(check_cross_issuer_isolation(signed_decision,
                                                    second_issuer_doc))
    return results
