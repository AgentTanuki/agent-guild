"""The one-call collaboration write path — how real, verifiable interactions
enter the canonical ledger.

prov-v2 (2026-07-13): one call = ONE party's cryptography. The record lands as
`mutual_attestation` (the requester's receipt-backed claim), and its `signers`
list names the requester only. `guild_mediated` requires two-party crypto,
escrow settlement, or a Guild-observed bound invocation — a requester must
never be able to manufacture the highest provenance class alone.
"""
import os
import hashlib

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.store import Store  # noqa: E402

client = TestClient(app)


def _register(name, caps):
    return client.post("/agents/register",
                       json={"name": name, "capabilities": caps}).json()


def test_one_call_records_one_party_mutual_attestation():
    req = _register("Hirer", ["hiring"])
    wkr = _register("Doer", ["summarize"])
    r = client.post("/collaborations",
                    headers={"X-API-Key": req["api_key"]},
                    json={"worker_id": wkr["id"], "capability": "summarize",
                          "outcome": "accepted", "rating": 0.95,
                          "deliverable": "a tidy three-sentence summary"})
    assert r.status_code == 200, r.text
    body = r.json()
    # prov-v2: one party's word can NEVER mint the highest provenance class,
    # and `signers` must not claim the worker signed anything.
    assert body["provenance"] == "mutual_attestation"
    assert body["ledger_record"]["signers"] == [req["did"]]
    assert wkr["did"] not in body["ledger_record"]["signers"]
    assert body["ledger_record"]["worker_id"] == wkr["id"]
    assert body["ledger_record"]["outcome"] == "accepted"
    # the deliverable was content-addressed server-side (sha256)
    expect = "0x" + hashlib.sha256(b"a tidy three-sentence summary").hexdigest()
    assert body["deliverable_hash"] == expect
    assert body["ledger_record"]["deliverable_hash"] == expect


def test_collaboration_shows_up_in_the_ledger():
    req = _register("Hirer2", ["hiring"])
    wkr = _register("Doer2", ["research"])
    before = client.get("/ledger/stats").json()["collaborations"]
    client.post("/collaborations", headers={"X-API-Key": req["api_key"]},
                json={"worker_id": wkr["id"], "capability": "research",
                      "outcome": "accepted", "rating": 0.9, "deliverable": "findings"})
    after = client.get("/ledger/stats").json()
    # stage-1: the one call lands the collab PLUS its raw typed events
    # (receipt, attestation) on the same chain
    assert after["collaborations"] == before + 1
    assert after["by_type"].get("receipt", 0) >= 1
    assert after["by_type"].get("attestation", 0) >= 1
    assert after["chain_valid"] is True
    # prov-v2: the one-call path lands at mutual_attestation, never guild_mediated
    assert after["by_provenance"].get("mutual_attestation", 0) >= 1


def test_requires_auth_and_rejects_self_and_bad_outcome():
    req = _register("Hirer3", ["hiring"])
    wkr = _register("Doer3", ["x"])
    # no key
    assert client.post("/collaborations",
                       json={"worker_id": wkr["id"], "capability": "x",
                             "outcome": "accepted", "rating": 1.0,
                             "deliverable": "d"}).status_code == 401
    # self-collaboration
    assert client.post("/collaborations", headers={"X-API-Key": req["api_key"]},
                       json={"worker_id": req["id"], "capability": "x",
                             "outcome": "accepted", "rating": 1.0,
                             "deliverable": "d"}).status_code == 400
    # invalid outcome
    assert client.post("/collaborations", headers={"X-API-Key": req["api_key"]},
                       json={"worker_id": wkr["id"], "capability": "x",
                             "outcome": "great", "rating": 1.0,
                             "deliverable": "d"}).status_code == 400


def test_store_helper_rejects_missing_deliverable():
    s = Store(path="")
    a = s.register_agent("R", ["h"], metadata={})
    b = s.register_agent("W", ["x"], metadata={})
    try:
        s.record_collaboration(a, b["id"], "x", "accepted", 1.0)
        assert False, "should require a deliverable or hash"
    except ValueError:
        pass
