"""Escrow + settlement — the economic layer.

Locks the revenue primitive: agents can lock value for work, settle it on
acceptance with the Guild taking a commission (its revenue on every transaction),
refund if undelivered, or dispute. Settlement also produces a payment-backed,
verifiable collaboration (tying the economic layer to the reputation moat).
"""
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.billing import FREE_CREDITS, settlement_fee  # noqa: E402

client = TestClient(app)


def _agent(name, caps=None):
    return client.post("/agents/register",
                       json={"name": name, "capabilities": caps or ["work"]}).json()


def _balance(key):
    return client.get("/billing/account", headers={"X-API-Key": key}).json()["balance"]


def test_full_settlement_pays_worker_and_earns_guild_fee():
    payer = _agent("Payer")
    worker = _agent("Worker")
    amount = 80
    fee = settlement_fee(amount)            # 2.5% of 80 -> 2

    # fund
    r = client.post("/escrow", headers={"X-API-Key": payer["api_key"]},
                    json={"worker_id": worker["id"], "amount": amount, "capability": "summarize"})
    assert r.status_code == 200, r.text
    esc = r.json()
    assert esc["status"] == "funded" and esc["fee"] == fee
    assert _balance(payer["api_key"]) == FREE_CREDITS - amount   # funds held
    assert esc["worker_risk"]["recommendation"] in ("hire", "caution", "avoid")

    # release -> worker paid (amount - fee), Guild keeps fee
    rel = client.post(f"/escrow/{esc['id']}/release",
                      headers={"X-API-Key": payer["api_key"]},
                      json={"deliverable": "a tidy summary", "rating": 0.95}).json()
    assert rel["status"] == "released"
    assert rel["payout"] == amount - fee
    assert _balance(worker["api_key"]) == FREE_CREDITS + (amount - fee)

    # revenue dashboard reflects the settled transaction
    rev = client.get("/billing/revenue").json()
    assert rev["settled_count"] == 1
    assert rev["settled_volume_credits"] == amount
    assert rev["guild_fee_credits"] == fee
    # sandbox credits are never presented as USD revenue
    assert "guild_revenue_usd" not in rev
    assert rev["currency"] == "credits_sandbox"
    assert rev["real_settlement"]["transactions"] == 0
    # settlement produced a verifiable, payment-backed collaboration
    assert rel["task_id"] is not None
    assert client.get("/ledger/stats").json()["records"] >= 1


def test_refund_returns_funds_and_charges_no_fee():
    payer = _agent("Payer2")
    worker = _agent("Worker2")
    esc = client.post("/escrow", headers={"X-API-Key": payer["api_key"]},
                      json={"worker_id": worker["id"], "amount": 50}).json()
    assert _balance(payer["api_key"]) == FREE_CREDITS - 50
    ref = client.post(f"/escrow/{esc['id']}/refund",
                      headers={"X-API-Key": payer["api_key"]}).json()
    assert ref["status"] == "refunded"
    assert _balance(payer["api_key"]) == FREE_CREDITS          # fully restored
    # cannot release a refunded escrow
    assert client.post(f"/escrow/{esc['id']}/release",
                       headers={"X-API-Key": payer["api_key"]},
                       json={}).status_code == 400


def test_only_payer_can_release_and_disputes_hold_funds():
    payer = _agent("Payer3")
    worker = _agent("Worker3")
    esc = client.post("/escrow", headers={"X-API-Key": payer["api_key"]},
                      json={"worker_id": worker["id"], "amount": 40}).json()
    # a non-payer (the worker) cannot release
    assert client.post(f"/escrow/{esc['id']}/release",
                       headers={"X-API-Key": worker["api_key"]},
                       json={}).status_code == 400
    # the worker CAN dispute; funds stay held
    d = client.post(f"/escrow/{esc['id']}/dispute",
                    headers={"X-API-Key": worker["api_key"]},
                    json={"grounds": "no payment intent"}).json()
    assert d["status"] == "disputed"
    assert _balance(payer["api_key"]) == FREE_CREDITS - 40     # still held


def test_guards_insufficient_credits_and_self_escrow():
    payer = _agent("Payer4")
    worker = _agent("Worker4")
    # more than the free balance
    assert client.post("/escrow", headers={"X-API-Key": payer["api_key"]},
                       json={"worker_id": worker["id"], "amount": FREE_CREDITS + 1}
                       ).status_code == 402
    # escrow to yourself
    assert client.post("/escrow", headers={"X-API-Key": payer["api_key"]},
                       json={"worker_id": payer["id"], "amount": 10}).status_code == 400
    # no auth
    assert client.post("/escrow", json={"worker_id": worker["id"], "amount": 10}
                       ).status_code == 401
