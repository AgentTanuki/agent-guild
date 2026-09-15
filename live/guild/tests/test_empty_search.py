"""Empty shortlists never consume money or sandbox credit."""
import pytest
from fastapi.testclient import TestClient
from app import main, payments, state, x402
from app.store import Store
from tests.test_x402_v2 import FakeFacilitator, make_payload, sig_header
from tests.test_payment_recovery_no_identifier import _anchor, _confirm_ok, fac, mainnet_store

@pytest.fixture(params=["json", "sqlite"])
def search_store(request, tmp_path, monkeypatch):
    for key, value in {"GUILD_STORE": request.param,
            "GUILD_STORE_PATH": str(tmp_path / "guild.sqlite3"),
            "GUILD_BILLING_ENFORCED": "1", "GUILD_X402_ENABLED": "1",
            "GUILD_X402_NETWORK": "eip155:84532",
            "GUILD_X402_PAY_TO": "0x" + "11" * 20}.items():
        monkeypatch.setenv(key, value)
    s = Store(path=str(tmp_path / "guild.json"))
    monkeypatch.setattr(main, "store", s)
    monkeypatch.setattr(state, "store", s)
    return s

def assert_empty(response, s):
    assert response.status_code == 200, response.text
    assert response.json() == {"capability": "paid-task", "count": 0, "results": []}
    assert response.headers["X-Guild-Cost"] == "0"
    assert "PAYMENT-REQUIRED" not in response.headers
    assert "PAYMENT-RESPONSE" not in response.headers
    assert not any(e["type"] == "best_agent_served" for e in s.events)
    assert any(e["type"] == "search_empty" and e["price_credits"] == 0 for e in s.events)
    assert any(e["type"] == "capability_demand" for e in s.events)

@pytest.mark.parametrize("credential", ["none", "x402", "malformed", "mpp"])
def test_empty_search_does_not_attempt_settlement(search_store, monkeypatch, credential):
    fac = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    headers = {}
    if credential == "x402":
        headers = {"PAYMENT-SIGNATURE": sig_header(make_payload(payments.search_request("paid-task")))}
    elif credential == "malformed":
        headers = {"PAYMENT-SIGNATURE": "malformed-test-only"}
    elif credential == "mpp":
        monkeypatch.setattr(main.mpp, "enabled", lambda: True)
        headers = {"Authorization": "Payment test-only"}
    response = TestClient(main.app).get("/search?capability=paid-task", headers=headers)
    assert_empty(response, search_store)
    assert fac.verify_calls == fac.settle_calls == []
    assert not search_store.x402_payment_ids

@pytest.mark.parametrize("filtered", [False, True])
def test_empty_search_preserves_sandbox_balance(search_store, filtered):
    if filtered:
        search_store.register_agent("Supplier", ["paid-task"], {})
    account = search_store.create_account()
    before = account["balance"]
    response = TestClient(main.app).get("/search?capability=paid-task&min_trust=100", headers={"X-API-Key": account["key"]})
    assert_empty(response, search_store)
    assert search_store.get_account(account["key"])["balance"] == before

def test_nonempty_search_still_requires_payment_and_charges_once(search_store):
    search_store.register_agent("Supplier", ["paid-task"], {})
    client = TestClient(main.app)
    quote = client.get("/search?capability=paid-task")
    assert quote.status_code == 402 and "PAYMENT-REQUIRED" in quote.headers
    account = search_store.create_account()
    before = account["balance"]
    response = client.get("/search?capability=paid-task", headers={"X-API-Key": account["key"]})
    cost = payments.search_request("paid-task").cost
    assert response.status_code == 200 and response.json()["count"] == 1
    assert search_store.get_account(account["key"])["balance"] == before - cost
    assert len([e for e in search_store.events if e["type"] == "best_agent_served"]) == 1

def test_paid_recovery_precedes_current_empty_search(mainnet_store, fac, monkeypatch):
    search_store = mainnet_store()
    monkeypatch.setattr(main, "store", search_store)
    _anchor(monkeypatch)
    _confirm_ok(monkeypatch)
    search_store.register_agent("Supplier", ["paid-task"], {})
    payload = make_payload(payments.search_request("paid-task"))
    headers = {"PAYMENT-SIGNATURE": sig_header(payload)}
    client = TestClient(main.app)
    paid = client.get("/search?capability=paid-task", headers=headers)
    assert paid.status_code == 200 and paid.json()["count"] == 1
    monkeypatch.setattr(search_store, "agents", {})
    monkeypatch.setattr(x402.time, "time", lambda: float(payload.payload["authorization"]["validBefore"]) + 3600)
    retry = client.get("/search?capability=paid-task", headers=headers)
    assert retry.status_code == 200 and retry.content == paid.content
    assert retry.headers["PAYMENT-RESPONSE"] == paid.headers["PAYMENT-RESPONSE"]
    assert retry.headers["X-Guild-Payment-Idempotent-Replay"] == "true"
    assert len(fac.settle_calls) == 1
