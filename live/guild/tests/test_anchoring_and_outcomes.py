"""Substantive checkpoint anchoring + AGO-1 signed outcomes
(corrective pass 2026-07-13).

* a decision's evidence counts ONLY what its cited checkpoint commits, and
  every counted record has a verifiable Merkle inclusion proof to that
  checkpoint's root;
* the checkpoint FEED is signed (entry_proof) with predecessor commitments,
  and legacy entries are bridged, never rewritten;
* outcomes are a server-side signed contract: requester-DID-signed, bound to
  gate envelope + provider DID + task + deliverable hash, sealed on the
  ledger, and verified by READBACK.
"""
from __future__ import annotations

import hashlib
import json
import os

os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app, store  # noqa: E402
from app.ledger import Ledger  # noqa: E402
from app.crypto import (generate_keypair, did_from_public_key,  # noqa: E402
                        sign_jcs, verify_jcs, canonicalize)

client = TestClient(app)


def _seed(cap="anchor-cap", n=3):
    w = client.post("/agents/register",
                    json={"name": "anchor-worker", "capabilities": [cap],
                          "metadata": {"endpoint": "https://a.example"}}).json()
    r = client.post("/agents/register",
                    json={"name": "anchor-req", "capabilities": []}).json()
    for i in range(n):
        client.post("/collaborations", headers={"X-API-Key": r["api_key"]},
                    json={"worker_id": w["id"], "capability": cap,
                          "outcome": "accepted", "rating": 0.9,
                          "deliverable": f"d{i}"})
    return w, r


# ------------------------------------------------------------ anchoring
def test_decision_evidence_is_checkpoint_committed_with_inclusion_proofs():
    w, r = _seed()
    out = client.get("/check", params={"capability": "anchor-cap"}).json()
    prov = out["decision"]["evidence_provenance"]
    assert prov["anchoring"] == "checkpoint_committed_only"
    cp = prov["checkpoint"]
    assert cp["index"] is not None and cp["head_hash"]
    # the cited checkpoint COMMITS every counted record — prove each one
    assert prov["verifiable_collaborations"] >= 3
    assert len(prov["record_ids"]) == prov["verifiable_collaborations"]
    for rid in prov["record_ids"]:
        proof = client.get(f"/ledger/inclusion/{rid}",
                           params={"checkpoint_index": cp["index"]}).json()
        rec = proof["record"]
        body = {k: v for k, v in rec.items() if k not in ("hash", "id")}
        leaf = hashlib.sha256(canonicalize(body).encode()).hexdigest()
        assert leaf == rec["hash"]
        assert Ledger.verify_merkle_proof(leaf, proof["path"],
                                          proof["checkpoint_merkle_root"])


def test_record_newer_than_checkpoint_is_not_committed():
    w, r = _seed(cap="anchor-cap2")
    cp = store.latest_checkpoint(publish_if_empty=False)
    # new evidence lands AFTER the checkpoint
    client.post("/collaborations", headers={"X-API-Key": r["api_key"]},
                json={"worker_id": w["id"], "capability": "anchor-cap2",
                      "outcome": "accepted", "rating": 0.9,
                      "deliverable": "late"})
    late = store.ledger_records[-1]
    resp = client.get(f"/ledger/inclusion/{late['id']}",
                      params={"checkpoint_index": cp["index"]})
    assert resp.status_code == 409  # NOT committed by that checkpoint


def test_feed_entries_are_signed_with_continuity():
    _seed(cap="anchor-cap3")
    client.get("/check", params={"capability": "anchor-cap3"})
    feed = client.get("/ledger/checkpoints").json()["checkpoints"]
    v2 = [e for e in feed if e.get("feed_version") == 2]
    assert v2, "expected feed_version-2 signed entries"
    gid_did = store.guild_identity()["did"]
    from app.crypto import public_key_from_did
    for e in v2:
        body = {k: v for k, v in e.items() if k != "entry_proof"}
        assert verify_jcs(body, e["entry_proof"],
                          public_key_from_did(gid_did))
        assert "prev_entry_sha256" in e


def test_legacy_feed_entries_get_a_signed_bridge_not_a_rewrite(monkeypatch):
    # simulating a pre-corrective legacy feed entry requires mutating the
    # in-memory checkpoint; the sqlite backend reloads from disk on publish
    # (which would restore the stripped fields), so this invariant is
    # exercised on the JSON store where the mutation is authoritative.
    monkeypatch.setenv("GUILD_STORE", "json")
    from app.store import Store
    s = Store(path="")
    w = s.register_agent("L", ["legacy-cap"], metadata={})
    r = s.register_agent("R", [], metadata={})
    s.record_collaboration(s.get_agent(r["id"]), w["id"], "legacy-cap",
                           "accepted", 0.9, deliverable="x")
    first = s.publish_checkpoint()
    # simulate a LEGACY entry: strip the modern fields (as pre-2026-07-13
    # deployments published them)
    for k in ("entry_proof", "prev_entry_sha256", "feed_version"):
        s.checkpoints[0].pop(k, None)
    legacy_bytes = hashlib.sha256(
        canonicalize(s.checkpoints[0]).encode()).hexdigest()
    s.record_collaboration(s.get_agent(r["id"]), w["id"], "legacy-cap",
                           "accepted", 0.9, deliverable="y")
    nxt = s.publish_checkpoint()
    assert nxt.get("bridge"), "first modern entry must bridge legacy entries"
    assert legacy_bytes in nxt["bridge"]["legacy_entry_sha256"]
    assert nxt["bridge"]["covers_indices"] == [first["index"]]
    # legacy entry itself untouched
    assert "entry_proof" not in s.checkpoints[0]


# ------------------------------------------------------------ AGO-1 outcomes
def _self_sovereign_requester():
    priv, pub = generate_keypair()
    did = did_from_public_key(pub)
    reg = client.post("/agents/register",
                      json={"name": "outcome-reporter", "capabilities": [],
                            "metadata": {}, "public_key": pub}).json()
    assert reg["did"] == did
    return reg, priv, did


def _outcome_doc(provider, requester_did, priv, **over):
    core = {
        "type": "AgentGuildOutcome",
        "contract": "AGO-1/1.0",
        "gate_envelope_sha256": "e" * 64,
        "provider_id": provider["id"],
        "provider_did": provider["did"],
        "endpoint_sha256": "sha256:" + "a" * 64,
        "capability": "anchor-cap",
        "task_ref": "task_x1",
        "deliverable_sha256": hashlib.sha256(b"bytes").hexdigest(),
        "outcome": "accepted",
        "reported_at": "2026-07-13T00:00:00+00:00",
        "requester_did": requester_did,
    }
    core.update(over)
    doc = dict(core)
    doc["proof"] = sign_jcs(core, priv)
    return doc


def test_signed_outcome_roundtrip_with_readback():
    provider, _r = _seed(cap="outcome-cap")
    provider = store.get_agent(provider["id"])
    reg, priv, did = _self_sovereign_requester()
    doc = _outcome_doc(provider, did, priv)
    res = client.post("/outcomes", json=doc)
    assert res.status_code == 200, res.text
    body = res.json()
    # READBACK: the record must be re-readable and hash-consistent
    rb = client.get(body["readback"]).json()["record"]
    assert rb["id"] == body["record_id"]
    assert rb["hash"] == body["ledger_hash"]
    assert rb["type"] == "signed_outcome"
    assert rb["body"]["provider_did"] == provider["did"]
    assert rb["body"]["gate_envelope_sha256"] == "e" * 64


def test_outcome_signer_must_control_registered_requester_did():
    provider, _ = _seed(cap="outcome-cap2")
    provider = store.get_agent(provider["id"])
    reg, priv, did = _self_sovereign_requester()
    # (a) unregistered DID
    other_priv, other_pub = generate_keypair()
    doc = _outcome_doc(provider, did_from_public_key(other_pub), other_priv)
    assert client.post("/outcomes", json=doc).status_code == 422
    # (b) registered DID but WRONG key (signer does not control it)
    doc = _outcome_doc(provider, did, other_priv)
    assert client.post("/outcomes", json=doc).status_code == 422
    # (c) tampered core after signing
    doc = _outcome_doc(provider, did, priv)
    doc["outcome"] = "rejected"
    assert client.post("/outcomes", json=doc).status_code == 422


def test_outcome_cannot_be_credited_to_a_different_provider():
    provider, _ = _seed(cap="outcome-cap3")
    provider = store.get_agent(provider["id"])
    other = client.post("/agents/register",
                        json={"name": "innocent-bystander",
                              "capabilities": [], "metadata": {}}).json()
    reg, priv, did = _self_sovereign_requester()
    # provider_id names one agent, provider_did another -> rejected
    doc = _outcome_doc(provider, did, priv,
                       provider_id=other["id"])   # DID stays provider's
    r = client.post("/outcomes", json=doc)
    assert r.status_code == 422
    assert "bound to a different identity" in r.text
