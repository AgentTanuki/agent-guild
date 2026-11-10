"""Public escrow reads never return the payer's account key (2.7.4).

Found by the first-party readiness walk of 2026-11-10: an escrow funded from a
trial account stored that account's key — which is its spendable credential —
as `requester_key`, and GET /escrow/{id} (no authentication) returned it.
"""
import json
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def _worker():
    return client.post("/agents/register",
                       json={"name": "PayerRefWorker", "capabilities": ["work"]}).json()


def _open(key, worker_id, amount=3):
    r = client.post("/escrow", headers={"X-API-Key": key},
                    json={"worker_id": worker_id, "amount": amount, "capability": "work"})
    assert r.status_code == 200, r.text
    return r.json()


def test_trial_funded_escrow_never_returns_the_payer_credential():
    worker = _worker()
    trial_key = client.post("/billing/trial").json()["key"]
    opened = _open(trial_key, worker["id"])
    read = client.get(f"/escrow/{opened['id']}")          # anonymous reader
    assert read.status_code == 200
    for body in (opened, read.json()):
        assert "requester_key" not in body
        assert trial_key not in json.dumps(body)
        assert body["payer_ref"].startswith("payer_")
    # the reference authenticates nothing
    assert client.get("/billing/account",
                      headers={"X-API-Key": read.json()["payer_ref"]}).status_code in (401, 404)


def test_agent_funded_escrow_never_returns_the_account_key_or_api_key():
    worker = _worker()
    payer = client.post("/agents/register",
                        json={"name": "PayerRefPayer", "capabilities": ["buy"]}).json()
    acct_key = client.get("/billing/account",
                          headers={"X-API-Key": payer["api_key"]}).json()["key"]
    opened = _open(payer["api_key"], worker["id"])
    text = json.dumps(opened) + client.get(f"/escrow/{opened['id']}").text
    assert "requester_key" not in text
    assert payer["api_key"] not in text and acct_key not in text


def test_payer_ref_is_stable_per_payer_and_distinct_across_payers():
    worker = _worker()
    k1 = client.post("/billing/trial").json()["key"]
    k2 = client.post("/billing/trial").json()["key"]
    a, b, c = _open(k1, worker["id"]), _open(k1, worker["id"]), _open(k2, worker["id"])
    assert a["payer_ref"] == b["payer_ref"] != c["payer_ref"]


def test_payer_can_still_release_and_refund_after_the_change():
    worker = _worker()
    k = client.post("/billing/trial").json()["key"]
    e1, e2 = _open(k, worker["id"]), _open(k, worker["id"])
    assert client.post(f"/escrow/{e1['id']}/release", headers={"X-API-Key": k},
                       json={"deliverable": "done", "rating": 1.0}).status_code == 200
    assert client.post(f"/escrow/{e2['id']}/refund",
                       headers={"X-API-Key": k}).json()["status"] == "refunded"
    assert client.get(f"/escrow/{e1['id']}").json()["status"] == "released"
