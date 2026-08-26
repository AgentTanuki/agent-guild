"""Caller-controlled preflight correlation without forged attribution."""

from fastapi.testclient import TestClient

from app import main


def _result():
    return {
        "verdict": "delegate_with_caution",
        "failed": [],
        "unknowns": [],
    }


def test_preflight_echoes_request_id_and_records_correlation(monkeypatch):
    monkeypatch.setattr(main.preflight, "run", lambda *_args, **_kw: _result())
    before = len(main.store.events)

    with TestClient(main.app) as client:
        response = client.get("/preflight", params={
            "url": "https://agent.example/a2a",
            "request_id": "pilot-reddit-ai-agents:7f13",
        })

    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == "pilot-reddit-ai-agents:7f13"
    assert body["request_id_semantics"] == (
        "caller_supplied_correlation_only_not_identity_or_attribution")

    events = [e for e in main.store.events[before:]
              if e.get("type") == "preflight_run"]
    assert len(events) == 1
    event = events[0]
    assert event["caller_request_id"] == "pilot-reddit-ai-agents:7f13"
    assert event["caller_request_id_semantics"] == (
        "correlation_only_not_identity_or_attribution")
    assert "community" not in event
    assert "source" not in event


def test_preflight_rejects_unsafe_request_id_before_network_work(monkeypatch):
    called = False

    def _run(*_args, **_kwargs):
        nonlocal called
        called = True
        return _result()

    monkeypatch.setattr(main.preflight, "run", _run)
    before = len(main.store.events)

    with TestClient(main.app) as client:
        response = client.get("/preflight", params={
            "url": "https://agent.example/a2a",
            "request_id": "reddit ai agents\nforged",
        })

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_request_id"
    assert called is False
    assert not [e for e in main.store.events[before:]
                if e.get("type") == "preflight_run"]


def test_preflight_without_request_id_preserves_response_shape(monkeypatch):
    monkeypatch.setattr(main.preflight, "run", lambda *_args, **_kw: _result())

    with TestClient(main.app) as client:
        response = client.get("/preflight", params={
            "url": "https://agent.example/a2a",
        })

    assert response.status_code == 200
    assert response.json() == _result()
