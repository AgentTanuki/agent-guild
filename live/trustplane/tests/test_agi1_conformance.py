"""AGI-1 conformance suite run against the real local Guild issuer, plus a
SECOND synthetic issuer to prove multi-issuer isolation, plus a manufactured
fork to prove fork detection actually fires."""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conformance.suite import (run_all, detect_fork,          # noqa: E402
                               check_feed_continuity,
                               check_cross_issuer_isolation)


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=15) as r:
        return json.loads(r.read().decode())


def _second_issuer_decision() -> dict:
    """An INDEPENDENT issuer implemented with the standalone primitives only
    (no Guild code): proves the standard is implementable by third parties."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from agentguild_trustplane.verify import canonicalize_jcs, b58encode
    import hashlib
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes_raw()
    did = "did:key:z" + b58encode(b"\xed\x01" + pub)
    now = datetime.now(timezone.utc)
    unsigned = {
        "type": "AgentGuildDecision", "contract": "AGD-1/1.0", "issuer": did,
        "capability": "tp-echo", "status": "supply",
        "issued_at": now.isoformat(),
        "valid_until": (now + timedelta(hours=1)).isoformat(),
        "decision": None, "routing": {"routable": False},
        "checkpoint": {"index": None},
    }
    proof = {"type": "DataIntegrityProof", "cryptosuite": "eddsa-jcs-2022",
             "created": now.isoformat(),
             "verificationMethod": f"did:key:{did[8:]}#{did[8:]}",
             "proofPurpose": "assertionMethod"}
    hd = (hashlib.sha256(canonicalize_jcs(proof).encode()).digest()
          + hashlib.sha256(canonicalize_jcs(unsigned).encode()).digest())
    proof["proofValue"] = "z" + b58encode(priv.sign(hd))
    return {**unsigned, "proof": proof}


def test_full_conformance_run(guild_server, seeded):
    base = guild_server["base"]
    signed = _get(base, "/check?capability=tp-echo&signed=true")
    passport = _get(base, f"/agents/{seeded['worker']['id']}/passport")
    seeded["call"]("POST", "/ledger/checkpoint/publish", {})
    feed = _get(base, "/ledger/checkpoints")
    entries = feed.get("checkpoints", feed) if isinstance(feed, dict) else feed
    results = run_all(signed, passport=passport, feed=entries,
                      second_issuer_doc=_second_issuer_decision(),
                      issuer_allowlist=[signed["issuer"]])
    failures = [r for r in results if not r["passed"]]
    assert not failures, failures
    assert len(results) >= 8


def test_fork_actually_detected(guild_server, seeded):
    seeded["call"]("POST", "/ledger/checkpoint/publish", {})
    feed = _get(guild_server["base"], "/ledger/checkpoints")
    entries = feed.get("checkpoints", feed) if isinstance(feed, dict) else feed
    pinned = entries[-1]
    forged = json.loads(json.dumps(pinned))
    forged["checkpoint"]["head_hash"] = "f" * 64
    r = detect_fork(pinned, forged)
    assert not r["passed"] and "FORK" in r["detail"]


def test_feed_tamper_detected(guild_server, seeded):
    feed = _get(guild_server["base"], "/ledger/checkpoints")
    entries = feed.get("checkpoints", feed) if isinstance(feed, dict) else feed
    if len(entries) < 2:
        seeded["call"]("POST", "/collaborations", key=seeded["requester"]["api_key"],
                       body={"worker_id": seeded["worker"]["id"],
                             "capability": "tp-echo", "outcome": "accepted",
                             "rating": 0.8, "deliverable": "extra"})
        seeded["call"]("POST", "/ledger/checkpoint/publish", {})
        feed = _get(guild_server["base"], "/ledger/checkpoints")
        entries = feed.get("checkpoints", feed) if isinstance(feed, dict) else feed
    assert check_feed_continuity(entries)["passed"]
    mutated = sorted(json.loads(json.dumps(entries)),
                     key=lambda e: e["index"])
    mutated[0]["ledger_length"] = 999999   # a PREDECESSOR someone commits to
    if len(mutated) >= 2:
        assert not check_feed_continuity(mutated)["passed"]


def test_issuer_isolation_is_real(guild_server, seeded):
    signed = _get(guild_server["base"], "/check?capability=tp-echo&signed=true")
    other = _second_issuer_decision()
    assert check_cross_issuer_isolation(signed, other)["passed"]
