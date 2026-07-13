"""AGD-1 decision contract — server-side pins (2026-07-13).

The `decision` object in /check is the STABLE machine contract of the trust
plane. These tests pin its shape and the signed-decision envelope so a
refactor cannot silently break every gateway in the field.
"""
from __future__ import annotations

import json
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

REQUIRED = ("contract", "agent_id", "identity", "capability_match",
            "estimate", "confidence", "staleness", "value_at_risk",
            "evidence_provenance", "policy", "reachability_status",
            "has_declared_endpoint")


def _seed(client):
    w = client.post("/agents/register",
                    json={"name": "agd-worker", "capabilities": ["agd-cap"],
                          "metadata": {"endpoint": "https://example.com/a2a"}}).json()
    r = client.post("/agents/register",
                    json={"name": "agd-req", "capabilities": []}).json()
    for i in range(3):
        client.post("/collaborations", headers={"X-API-Key": r["api_key"]},
                    json={"worker_id": w["id"], "capability": "agd-cap",
                          "outcome": "accepted", "rating": 0.9,
                          "deliverable": f"d{i}"})
    return w, r


def test_decision_is_agd1():
    _seed(client)
    out = client.get("/check", params={"capability": "agd-cap"}).json()
    d = out["decision"]
    assert d["contract"] == "AGD-1/1.0"
    for f in REQUIRED:
        assert f in d, f"AGD-1 field missing: {f}"
    ident = d["identity"]
    assert ident["did"].startswith("did:key:")
    assert isinstance(ident["did_control_proven"], bool)
    var = d["value_at_risk"]
    assert set(var["tiers"]) == {"micro", "low", "medium", "high"}
    prov = d["evidence_provenance"]
    assert prov["rules_version"] == "prov-v2"
    assert prov["verifiable_collaborations"] >= 1
    # the policy slot belongs to the caller — the server never fills it
    assert d["policy"]["result"] is None
    assert d["policy"]["decided_by"] == "caller"
    # legacy demotion is explicit
    assert "contract_note" in out
    assert "deprecated" in out["verdict"]


def test_signed_decision_verifies_and_tamper_fails():
    _seed(client)
    sd = client.get("/check", params={"capability": "agd-cap",
                                      "signed": "true",
                                      "ttl_seconds": "300"}).json()
    assert sd["type"] == "AgentGuildDecision"
    assert sd["proof"]["cryptosuite"] == "eddsa-jcs-2022"
    assert sd["valid_until"] > sd["issued_at"]
    from app.crypto import verify_eddsa_jcs, public_key_from_did
    doc = json.loads(json.dumps(sd))
    proof = doc.pop("proof")
    pv = proof.pop("proofValue")
    pub = public_key_from_did(doc["issuer"])
    assert verify_eddsa_jcs(doc, proof, pv, pub)
    doc["decision"]["estimate"] = 0.99
    assert not verify_eddsa_jcs(doc, proof, pv, pub)


def test_no_supply_signed_decision_is_still_signed():
    sd = client.get("/check", params={"capability": "never-supplied-cap",
                                      "signed": "true"}).json()
    assert sd["decision"] is None and sd["status"] == "no_supply_yet"
    assert sd["proof"]["proofValue"].startswith("z")
