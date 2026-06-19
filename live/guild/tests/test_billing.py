"""Billing tests — credit ledger, metering, enforcement, and top-ups."""
import os
os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.billing import FREE_CREDITS, PRICING  # noqa: E402

client = TestClient(app)


def _agent(name="Buyer", caps=None):
    r = client.post("/agents/register", json={"name": name, "capabilities": caps or ["research"]})
    return r.json()


def test_new_account_has_free_credits_and_pricing():
    a = _agent("Acct")
    r = client.get("/billing/account", headers={"X-API-Key": a["api_key"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["balance"] == FREE_CREDITS
    assert body["pricing"]["best_agent"] == PRICING["best_agent"]


def test_paid_lookup_draws_down_credits_and_sets_headers():
    a = _agent("Searcher")
    # baseline balance
    bal0 = client.get("/billing/account", headers={"X-API-Key": a["api_key"]}).json()["balance"]
    r = client.get("/search", params={"capability": "research"},
                   headers={"X-API-Key": a["api_key"]})
    assert r.status_code == 200, r.text
    assert r.headers["X-Guild-Cost"] == str(PRICING["best_agent"])
    assert int(r.headers["X-Guild-Balance"]) == bal0 - PRICING["best_agent"]


def test_writes_are_free():
    a = _agent("Writer")
    subject = _agent("Subject", ["fact-check"])
    before = client.get("/billing/account", headers={"X-API-Key": a["api_key"]}).json()["balance"]
    client.post("/attestations", headers={"X-API-Key": a["api_key"]},
                json={"issuer_id": a["id"], "subject_id": subject["id"],
                      "capability": "fact-check", "rating": 0.8})
    after = client.get("/billing/account", headers={"X-API-Key": a["api_key"]}).json()["balance"]
    assert before == after  # attesting costs nothing


def test_insufficient_credits_returns_402():
    acct = client.post("/billing/account").json()
    key = acct["key"]
    # drain: FREE_CREDITS / 10 best-agent lookups, then one more must 402
    n = FREE_CREDITS // PRICING["best_agent"]
    for _ in range(n):
        r = client.get("/search", params={"capability": "research"}, headers={"X-API-Key": key})
        assert r.status_code == 200
    r = client.get("/search", params={"capability": "research"}, headers={"X-API-Key": key})
    assert r.status_code == 402, r.text


def test_dev_topup_requires_token_then_credits():
    os.environ["GUILD_BILLING_DEV_TOKEN"] = "testdev"
    try:
        acct = client.post("/billing/account").json()
        key = acct["key"]
        # wrong token rejected
        r = client.post("/billing/topup", headers={"X-API-Key": key},
                        json={"credits": 500, "dev_token": "nope"})
        assert r.status_code == 403
        # correct token credits the account
        r = client.post("/billing/topup", headers={"X-API-Key": key},
                        json={"credits": 500, "dev_token": "testdev"})
        assert r.status_code == 200, r.text
        assert r.json()["balance"] == FREE_CREDITS + 500
    finally:
        os.environ.pop("GUILD_BILLING_DEV_TOKEN", None)


def test_enforcement_requires_funded_key():
    os.environ["GUILD_BILLING_ENFORCED"] = "1"
    try:
        # no key at all -> 402
        r = client.get("/search", params={"capability": "research"})
        assert r.status_code == 402, r.text
        # a funded account -> ok
        acct = client.post("/billing/account").json()
        r = client.get("/search", params={"capability": "research"},
                       headers={"X-API-Key": acct["key"]})
        assert r.status_code == 200, r.text
    finally:
        os.environ.pop("GUILD_BILLING_ENFORCED", None)
