"""Privacy-safe, machine-returnable receipts for free preflight runs."""
import re

from fastapi.testclient import TestClient

from app import main
from app.main import app
from app.state import store


client = TestClient(app)


def _result(url: str, **_kwargs):
    return {
        "target": url,
        "verdict": "delegate_with_caution",
        "checks": [],
        "failed": [],
        "unknowns": ["independent_evidence"],
    }


def test_preflight_returns_server_issued_receipt_and_public_event_joins_it(
        monkeypatch):
    monkeypatch.setattr(main.preflight, "run", _result)
    target = "https://counterparty.example/a2a"

    response = client.get("/preflight", params={"url": target}, headers={
        "User-Agent": "receipt-test-machine/1.0",
    })

    assert response.status_code == 200
    observation_id = response.json()["observation_id"]
    assert re.fullmatch(r"pfobs_[0-9a-f]{32}", observation_id)

    raw = next(e for e in reversed(store.events)
               if e.get("observation_id") == observation_id)
    assert raw["type"] == "preflight_run"
    assert raw["target"] == target

    public = client.get("/instrumentation/recent", params={"limit": 20}).json()
    event = next(e for e in public["events"]
                 if e.get("observation_id") == observation_id)
    assert event["type"] == "preflight_run"
    assert event["user_agent"] == "receipt-test-machine/1.0"
    assert "target" not in event


def test_preflight_receipt_is_unique_and_cannot_be_caller_selected(monkeypatch):
    monkeypatch.setattr(main.preflight, "run", _result)
    supplied = "pfobs_" + "0" * 32

    first = client.get("/preflight", params={
        "url": "https://one.example/a2a",
        "observation_id": supplied,
    }).json()["observation_id"]
    second = client.get("/preflight", params={
        "url": "https://two.example/a2a",
    }).json()["observation_id"]

    assert first != supplied
    assert second != supplied
    assert first != second


def test_recent_feed_keeps_legacy_events_compatible_without_receipt():
    store.record_event(None, "query", ua="legacy-machine/1.0",
                       endpoint="best_agent")
    event = store.recent_events(limit=1)[0]
    assert event["observation_id"] is None


def test_llms_contract_explains_receipt_is_correlation_not_identity():
    body = client.get("/llms.txt").text
    assert "server-issued observation_id" in body
    assert "does not prove caller identity" in body
