"""Natural objectives map compactly without bypassing the priced trust read."""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import pricing  # noqa: E402
from app.store import Store  # noqa: E402

EXT_HDR = {
    "user-agent": "external-agent-framework/3.2 (langchain)",
    "X-A2A-Extensions": "https://github.com/google-a2a/a2a-x402/v0.1",
}


@pytest.fixture()
def store(tmp_path, monkeypatch) -> Store:
    pricing.load_runtime({})
    result = Store(path=str(tmp_path / "guild.json"))
    import app.main as main_mod
    import app.a2a as a2a_mod
    import app.a2a_x402 as a2a_x402_mod
    import app.state as state_mod
    monkeypatch.setattr(main_mod, "store", result)
    monkeypatch.setattr(a2a_mod, "store", result)
    monkeypatch.setattr(a2a_x402_mod, "store", result)
    monkeypatch.setattr(state_mod, "store", result)
    return result


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


@pytest.fixture()
def x402_on(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)


def _send(client, text):
    return client.post("/a2a", headers=EXT_HDR, json={
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {"role": "user",
                               "parts": [{"kind": "text", "text": text}],
                               "messageId": "m-" + uuid.uuid4().hex[:8]}}})


def _payment_required(resp_json):
    result = resp_json.get("result") or {}
    message = (result.get("status") or {}).get("message") or {}
    return (message.get("metadata") or {}).get("x402.payment.required")


@pytest.mark.parametrize("text, capability", [
    ("I need an agent for fact-check", "fact-check"),
    ("find an agent for summarize", "summarize"),
    ("Looking for an agent to do translation", "translation"),
    ("recommend an agent for code-review", "code-review"),
    ("who can do fact-check? I need an agent for fact-check today", "fact-check"),
])
def test_natural_language_ask_maps_to_explicit_priced_action(
        store, client, x402_on, text, capability):
    response = _send(client, text)
    assert response.status_code == 200, response.text
    assert _payment_required(response.json()) is None
    result = response.json()["result"]
    raw = result["parts"][0]["text"].encode()
    assert len(raw) < 1024
    payload = json.loads(raw)
    assert payload["kind"] == "objective_match"
    assert payload["match"]["canonical_capability"] == capability
    assert payload["result"] == {
        "status": "mapping_only", "trust_decision": "not_included"}
    action = payload["available_actions"][0]
    assert action["id"] == "trust.check.full"
    assert action["effect"] == "metered_read"
    assert action["requires_local_authorisation"] is True
    assert action["price_credits"] > 0

    demand = [event for event in store.events
              if event.get("type") == "capability_demand"]
    assert demand and demand[0].get("capability") == capability

    shown = [event for event in store.events
             if event.get("type") == "paid_offer_shown"
             and event.get("challenged_operation") == "best_agent"]
    assert len(shown) == 1
    assert shown[0]["impression"] == "action_link"


def test_keyword_syntax_still_works(store, client, x402_on):
    response = _send(client, "hire: fact-check")
    assert _payment_required(response.json()) is not None


def test_capability_name_does_not_become_an_explicit_check_verb(
        store, client, x402_on):
    _send(client, "I need an agent for fact-check today")
    demand = [event for event in store.events
              if event.get("type") == "capability_demand"]
    assert demand and demand[0].get("capability") == "fact-check"


@pytest.mark.parametrize("text", [
    "I need an agent",
    "find me an agent to do the task",
    "agents are interesting",
    "ping",
    "I need someone to help",
])
def test_non_asks_and_stopwords_stay_unpriced(
        store, client, x402_on, text):
    response = _send(client, text)
    assert response.status_code == 200
    assert _payment_required(response.json()) is None, (
        f"over-broad match would fabricate demand: {text!r}")
