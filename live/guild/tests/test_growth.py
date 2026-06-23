"""Tests for the growth + self-evaluation layer (Outcomes 1 & 4).

Covers the referral primitive (record at registration, pay the referrer only on
the referred agent's activation, cap abuse) and the continuous self-evaluation
snapshot/trend mechanism.
"""
import copy
import os
import pytest
from fastapi.testclient import TestClient

os.environ.pop("GUILD_ADMIN_TOKEN", None)  # keep self-eval recording open in tests
from app.main import app  # noqa: E402
from app.billing import REFERRAL_REWARD_CREDITS  # noqa: E402

_COLLECTIONS = ("agents", "tasks", "attestations", "accounts",
                "billing_log", "events", "referrals", "health_log")


@pytest.fixture
def client():
    """Run each test against an empty store, then restore the shared singleton's
    prior contents so the rest of the (accumulation-based) suite is unaffected."""
    from app.state import store
    saved = {name: copy.deepcopy(getattr(store, name)) for name in _COLLECTIONS}
    saved_cache = store._rep_cache
    for name in _COLLECTIONS:
        getattr(store, name).clear()
    store._rep_cache = None
    try:
        yield TestClient(app)
    finally:
        for name in _COLLECTIONS:
            col = getattr(store, name)
            col.clear()
            if isinstance(col, list):
                col.extend(saved[name])
            else:
                col.update(saved[name])
        store._rep_cache = saved_cache


def _register(client, name, caps, **extra):
    return client.post("/agents/register", json={"name": name, "capabilities": caps, **extra}).json()


def test_referral_recorded_but_not_paid_until_activation(client):
    referrer = _register(client, "Referrer", ["research"])
    bal0 = client.get("/billing/account", headers={"X-API-Key": referrer["api_key"]}).json()["balance"]

    worker = _register(client, "Worker", ["fact-check"], referred_by=referrer["id"])
    assert worker["referred_by"] == referrer["id"]

    refs = client.get("/referrals").json()
    assert refs["total_referrals"] == 1
    assert refs["activated_referrals"] == 0  # not activated by mere registration

    bal1 = client.get("/billing/account", headers={"X-API-Key": referrer["api_key"]}).json()["balance"]
    assert bal1 == bal0  # referrer NOT paid yet


def test_referrer_paid_on_worker_delivering_receipt(client):
    referrer = _register(client, "Referrer", ["research"])
    bal0 = client.get("/billing/account", headers={"X-API-Key": referrer["api_key"]}).json()["balance"]
    worker = _register(client, "Worker", ["fact-check"], referred_by=referrer["id"])
    hirer = _register(client, "Hirer", ["x"])

    t = client.post("/tasks", json={"requester_id": hirer["id"], "worker_id": worker["id"],
                                    "task_type": "fact-check", "payment": 1.0},
                    headers={"X-API-Key": hirer["api_key"]}).json()
    client.post(f"/tasks/{t['id']}/receipt", json={"deliverable_hash": "h", "outcome": "accepted"},
                headers={"X-API-Key": worker["api_key"]})

    refs = client.get("/referrals").json()
    assert refs["activated_referrals"] == 1
    bal1 = client.get("/billing/account", headers={"X-API-Key": referrer["api_key"]}).json()["balance"]
    assert bal1 - bal0 == REFERRAL_REWARD_CREDITS


def test_referrer_paid_only_once(client):
    referrer = _register(client, "Referrer", ["research"])
    bal0 = client.get("/billing/account", headers={"X-API-Key": referrer["api_key"]}).json()["balance"]
    worker = _register(client, "Worker", ["fact-check"], referred_by=referrer["id"])
    hirer = _register(client, "Hirer", ["x"])
    for _ in range(3):  # multiple receipts must not pay the referrer repeatedly
        t = client.post("/tasks", json={"requester_id": hirer["id"], "worker_id": worker["id"],
                                        "task_type": "fact-check", "payment": 1.0},
                        headers={"X-API-Key": hirer["api_key"]}).json()
        client.post(f"/tasks/{t['id']}/receipt", json={"deliverable_hash": "h", "outcome": "accepted"},
                    headers={"X-API-Key": worker["api_key"]})
    bal1 = client.get("/billing/account", headers={"X-API-Key": referrer["api_key"]}).json()["balance"]
    assert bal1 - bal0 == REFERRAL_REWARD_CREDITS  # exactly one reward


def test_bogus_referrer_is_ignored(client):
    worker = _register(client, "Worker", ["fact-check"], referred_by="agent_doesnotexist")
    assert worker["referred_by"] is None
    assert client.get("/referrals").json()["total_referrals"] == 0


def test_self_eval_snapshot_and_trend(client):
    _register(client, "Seed", ["fact-check"], seed=True)
    s1 = client.post("/self-eval/run").json()
    assert s1["agents_total"] == 1
    assert "verdict" in s1 and s1["verdict"]

    _register(client, "W", ["fact-check"])
    s2 = client.post("/self-eval/run").json()
    assert s2["agents_total"] == 2
    assert s2["deltas"].get("agents_total") == 1  # trend captured

    hist = client.get("/self-eval/history").json()
    assert hist["count"] == 2


def test_self_eval_verdict_is_honest_when_empty(client):
    snap = client.post("/self-eval/run").json()
    assert "NO EXTERNAL AGENTS YET" in snap["verdict"]
