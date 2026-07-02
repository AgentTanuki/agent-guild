"""Stage 0 of the Trust Graph migration (docs/TRUST_GRAPH_GAP_ANALYSIS.md).

Locks three guarantees:
  1. Identity primitives — behavioral configuration + principal are captured at
     registration, changeable via a declared config event, and STAMPED onto every
     evidence record at write time (white paper §3.2, §7.3).
  2. Schema v2 — trust is never a bare scalar: /check, /reputation, /risk-score
     expose estimate + confidence + staleness + explanation; v1 fields remain
     (additive, non-breaking) and are marked deprecated (§6.1, §10).
  3. Upheld challenges are NEGATIVE evidence — an upheld challenge counts the
     record as a failure at full provenance weight, never weight-zero (§6.4).
"""
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.state import store  # noqa: E402
from app.ledger import Ledger, CollaborationRecord, PROVENANCE_WEIGHT  # noqa: E402

client = TestClient(app)

CFG_V1 = {"model": "test-model-v1", "constitution_hash": "abc123", "tools": ["search"]}
CFG_V2 = {"model": "test-model-v2", "constitution_hash": "abc123", "tools": ["search"]}


def _register(name, caps, config=None, principal=None):
    r = client.post("/agents/register", json={
        "name": name, "capabilities": caps,
        **({"config": config} if config else {}),
        **({"principal": principal} if principal else {}),
    })
    assert r.status_code == 200, r.text
    return r.json()


# --- 1. identity primitives --------------------------------------------------

def test_registration_captures_config_and_principal():
    a = _register("cfg-agent", ["fact-check"], config=CFG_V1, principal="org:test-corp")
    assert a["config_hash"] and len(a["config_hash"]) == 64
    prof = client.get(f"/agents/{a['id']}").json()
    assert prof["config_hash"] == a["config_hash"]
    assert prof["principal"] == "org:test-corp"
    assert prof["config_changes"] == 0


def test_registration_without_config_is_fine_and_null():
    a = _register("bare-agent", ["fact-check"])
    assert a["config_hash"] is None
    prof = client.get(f"/agents/{a['id']}").json()
    assert prof["config_hash"] is None and prof["principal"] is None


def test_config_hash_is_content_addressed_and_stable():
    h1 = store.config_hash_of(CFG_V1)
    h2 = store.config_hash_of(dict(reversed(list(CFG_V1.items()))))  # key order irrelevant
    assert h1 == h2
    assert store.config_hash_of(CFG_V2) != h1


def test_declare_configuration_requires_agent_key_and_records_history():
    a = _register("rotating-agent", ["fact-check"], config=CFG_V1)
    # wrong key rejected
    r = client.post(f"/agents/{a['id']}/configuration",
                    json={"config": CFG_V2}, headers={"X-API-Key": "sk_wrong"})
    assert r.status_code == 401
    # right key accepted
    r = client.post(f"/agents/{a['id']}/configuration",
                    json={"config": CFG_V2}, headers={"X-API-Key": a["api_key"]})
    assert r.status_code == 200
    body = r.json()
    assert body["previous_hash"] == a["config_hash"]
    assert body["config_hash"] != a["config_hash"]
    assert body["config_changes"] == 1
    prof = client.get(f"/agents/{a['id']}").json()
    assert prof["config_hash"] == body["config_hash"]
    assert prof["config_changes"] == 1


def test_evidence_records_are_stamped_with_config_at_write_time():
    req = _register("stamp-requester", ["hiring"], config=CFG_V1)
    wrk = _register("stamp-worker", ["fact-check"], config=CFG_V1)
    t = client.post("/tasks", json={
        "requester_id": req["id"], "worker_id": wrk["id"],
        "task_type": "fact-check", "payment": 5.0,
    }, headers={"X-API-Key": req["api_key"]})
    assert t.status_code == 200
    task = store.get_task(t.json()["id"])
    wrk_hash_v1 = wrk["config_hash"]
    assert task["worker_config_hash"] == wrk_hash_v1
    assert task["requester_config_hash"] == req["config_hash"]
    # worker swaps config; NEW evidence carries the new hash, old keeps the old
    client.post(f"/agents/{wrk['id']}/configuration",
                json={"config": CFG_V2}, headers={"X-API-Key": wrk["api_key"]})
    t2 = client.post("/tasks", json={
        "requester_id": req["id"], "worker_id": wrk["id"],
        "task_type": "fact-check", "payment": 5.0,
    }, headers={"X-API-Key": req["api_key"]})
    task2 = store.get_task(t2.json()["id"])
    assert task2["worker_config_hash"] == store.config_hash_of(CFG_V2)
    assert task["worker_config_hash"] == wrk_hash_v1  # history is immutable


def test_attestations_are_stamped_with_config():
    issuer = _register("att-issuer", ["hiring"], config=CFG_V1)
    subject = _register("att-subject", ["fact-check"], config=CFG_V2)
    r = client.post("/attestations", json={
        "issuer_id": issuer["id"], "subject_id": subject["id"],
        "capability": "fact-check", "rating": 0.9,
    }, headers={"X-API-Key": issuer["api_key"]})
    assert r.status_code == 200
    att = store.attestations_for(subject["id"])[-1]
    assert att["issuer_config_hash"] == issuer["config_hash"]
    assert att["subject_config_hash"] == subject["config_hash"]


# --- 2. schema v2: never a bare scalar ---------------------------------------

def _agent_with_reputation():
    req = _register("v2-requester", ["hiring"])
    wrk = _register("v2-worker", ["schema-check"])
    r = client.post("/collaborations", json={
        "worker_id": wrk["id"], "capability": "schema-check",
        "outcome": "accepted", "rating": 0.95,
        "deliverable": "the work product", "payment": 10.0,
    }, headers={"X-API-Key": req["api_key"]})
    assert r.status_code == 200, r.text
    return wrk


def test_reputation_response_is_v2():
    wrk = _agent_with_reputation()
    rep = client.get(f"/agents/{wrk['id']}/reputation").json()
    assert rep["schema_version"] == 2
    assert 0.0 <= rep["estimate"] <= 1.0
    assert "confidence" in rep
    assert rep["staleness"] is None            # honest: decay not computed yet
    assert isinstance(rep["explanation"], list) and rep["explanation"]
    assert "trust" in rep and "rank" in rep    # v1 fields still present


def test_risk_score_is_v2_and_keeps_deprecated_v1_fields():
    wrk = _agent_with_reputation()
    rs = client.get(f"/agents/{wrk['id']}/risk-score").json()
    assert rs["schema_version"] == 2
    assert 0.0 <= rs["estimate"] <= 1.0
    assert isinstance(rs["explanation"], list) and rs["explanation"]
    # deprecated but not broken:
    assert rs["recommendation"] in ("hire", "caution", "avoid")
    assert 0.0 <= rs["risk"] <= 100.0


def test_check_verdict_is_v2_and_marks_deprecations():
    wrk = _agent_with_reputation()
    out = client.get("/check", params={"capability": "schema-check"}).json()
    assert out["schema_version"] == 2
    v = out["verdict"]
    assert v is not None
    assert v["schema_version"] == 2
    assert 0.0 <= v["estimate"] <= 1.0
    assert isinstance(v["explanation"], list) and v["explanation"]
    assert set(v["deprecated"]) == {"risk", "recommendation", "trust"}
    assert v["recommendation"] in ("hire", "caution", "avoid")  # still served


def test_explanation_lines_reference_checkable_evidence():
    wrk = _agent_with_reputation()
    rep = client.get(f"/agents/{wrk['id']}/reputation").json()
    text = " ".join(rep["explanation"])
    assert "receipt" in text            # points at verifiable receipts
    assert "reviewer" in text           # points at issuer structure
    assert "Staleness" in text          # honest about what is NOT computed


# --- 3. upheld challenge = negative evidence, never erasure -------------------

def _record(outcome="accepted", provenance="guild_mediated", challenge="none"):
    return CollaborationRecord(
        seq=0, requester_did="did:r", worker_did="did:w",
        requester_id="r", worker_id="w", capability="x", task_id="t",
        outcome=outcome, deliverable_hash="h", payment=1.0, stake=0.0,
        provenance=provenance, signers=[], evidence={}, created_at="now",
        prev_hash="0" * 64, challenge_status=challenge,
    )


def test_upheld_challenge_counts_as_failure_at_full_weight():
    led = Ledger()
    led.append(_record(outcome="accepted", challenge="upheld"))
    rep = led.derive_reputation()["w"]
    # the record still counts (weight > 0) and counts AGAINST (success rate 0)
    assert rep["weighted_total"] == PROVENANCE_WEIGHT["guild_mediated"]
    assert rep["verifiable_success_rate"] == 0.0


def test_upheld_fraud_is_not_diluted_to_invisibility():
    """One adjudicated fault among successes must depress the rate, not vanish."""
    led = Ledger()
    for _ in range(3):
        led.append(_record(outcome="accepted"))
    led.append(_record(outcome="accepted", challenge="upheld"))
    rep = led.derive_reputation()["w"]
    assert rep["verifiable_success_rate"] == 0.75  # 3/4 — the fault is priced in


def test_open_challenge_still_downweights_pending_resolution():
    led = Ledger()
    led.append(_record(outcome="accepted", challenge="open"))
    rep = led.derive_reputation()["w"]
    assert 0 < rep["weighted_total"] < PROVENANCE_WEIGHT["guild_mediated"]
