"""AGD-1 decision contract — server-side pins (2026-07-13).

The `decision` object in /check is the STABLE machine contract of the trust
plane. These tests pin its shape and the signed-decision envelope so a
refactor cannot silently break every gateway in the field.
"""
from __future__ import annotations

import asyncio
import json
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

REQUIRED = ("contract", "agent_id", "identity", "capability_match",
            "estimate", "confidence", "staleness", "freshness", "value_at_risk",
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
    fresh = d["freshness"]
    assert fresh["contract"] == "AGD-1/freshness-1"
    assert fresh["global_clock"] is None
    assert set(fresh["classes"]) == {
        "competence_outcomes", "capability_liveness",
        "endpoint_reachability",
        "reputation_attestations", "identity_control",
        "settlement_finality", "upheld_fraud",
    }
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

    freshness_doc = json.loads(json.dumps(sd))
    freshness_proof = freshness_doc.pop("proof")
    freshness_pv = freshness_proof.pop("proofValue")
    freshness_doc["decision"]["freshness"]["classes"][
        "competence_outcomes"]["age_seconds"] = 0
    assert not verify_eddsa_jcs(
        freshness_doc, freshness_proof, freshness_pv, pub)


def test_no_supply_signed_decision_is_still_signed():
    sd = client.get("/check", params={"capability": "never-supplied-cap",
                                      "signed": "true"}).json()
    assert sd["decision"] is None and sd["status"] == "no_supply_yet"
    assert sd["proof"]["proofValue"].startswith("z")


def test_freshness_standard_is_validator_visible():
    out = client.get("/standard/freshness")
    assert out.status_code == 200
    body = out.json()
    assert body["contract"] == "AGD-1/freshness-1"
    schema = body["json_schema"]
    assert schema["properties"]["contract"]["const"] == "AGD-1/freshness-1"
    classes_ref = schema["properties"]["classes"]["$ref"]
    classes_name = classes_ref.rsplit("/", 1)[-1]
    required = set(schema["$defs"][classes_name]["required"])
    assert required == {
        "competence_outcomes", "capability_liveness",
        "endpoint_reachability", "reputation_attestations",
        "identity_control", "settlement_finality", "upheld_fraud",
    }

    openapi = client.get("/openapi.json").json()
    assert "/standard/freshness" in openapi["paths"]
    rep = openapi["components"]["schemas"]["ReputationResponse"]
    risk = openapi["components"]["schemas"]["RiskScoreResponse"]
    evidence = openapi["components"]["schemas"]["EvidenceResponse"]
    expected = "#/components/schemas/SourceSeparatedFreshness"
    assert rep["properties"]["freshness"]["$ref"] == expected
    assert risk["properties"]["freshness"]["$ref"] == expected
    assert evidence["properties"]["freshness"]["$ref"] == expected
    for path in ("/check", "/standard/freshness",
                 "/agents/{agent_id}/passport"):
        schema = openapi["paths"][path]["get"]["responses"]["200"][
            "content"]["application/json"]["schema"]
        assert schema != {}

    from app.mcp_server import mcp
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    for name in ("guild_check", "guild_risk_score"):
        output = tools[name].to_mcp_tool().model_dump(
            by_alias=True, exclude_none=True)["outputSchema"]
        assert output.get("properties"), name
        assert "freshness" in json.dumps(output)


def test_http_trust_reads_serve_the_typed_freshness_shape():
    worker, _ = _seed(client)
    headers = {"X-API-Key": worker["api_key"]}
    reputation = client.get(
        f"/agents/{worker['id']}/reputation", headers=headers)
    risk = client.get(f"/agents/{worker['id']}/risk-score", headers=headers)
    evidence = client.get(
        f"/agents/{worker['id']}/evidence", headers=headers)
    assert reputation.status_code == 200
    assert risk.status_code == 200
    assert evidence.status_code == 200
    for body in (reputation.json(), risk.json(), evidence.json()):
        assert body["freshness"]["contract"] == "AGD-1/freshness-1"
        assert body["freshness"]["global_clock"] is None
        assert set(body["freshness"]["classes"]) == {
            "competence_outcomes", "capability_liveness",
            "endpoint_reachability", "reputation_attestations",
            "identity_control", "settlement_finality", "upheld_fraud",
        }
