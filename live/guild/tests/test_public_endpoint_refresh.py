"""Public stored-endpoint refresh route: no credential, no redirect authority."""
import os
import tempfile
from unittest import mock

os.environ.setdefault("GUILD_DATA", "")
os.environ.setdefault("GUILD_BOOTSTRAP_EVAL", "0")

from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_mod  # noqa: E402
from app import reachability as R  # noqa: E402
from app.store import Store  # noqa: E402


def _store():
    return Store(path=os.path.join(tempfile.mkdtemp(), "refresh.json"))


def test_public_route_refreshes_without_key_and_cannot_take_endpoint(monkeypatch):
    store = _store()
    agent = store.register_agent("Public crank", ["research"], {})
    endpoint = "https://worker.example/a2a"
    store.set_agent_endpoint(agent["id"], endpoint)
    monkeypatch.setattr(main_mod, "store", store)

    card = b'{"protocolVersion":"0.3.0","skills":[{"id":"research"}]}'
    with mock.patch.object(R.socket, "getaddrinfo", return_value=[
            (R.socket.AF_INET, 1, 6, "", ("93.184.216.34", 443))]), \
         mock.patch.object(R, "_http_request_pinned", return_value=(200, card)), \
         TestClient(main_mod.app) as client:
        response = client.post(f"/agents/{agent['id']}/endpoint/refresh")
        attempted_redirect = client.post(
            f"/agents/{agent['id']}/endpoint/refresh",
            json={"endpoint": "https://attacker.example/a2a"})

    assert response.status_code == 200, response.text
    assert response.json()["refresh_performed"] is True
    assert response.json()["recommended_for_routing"] is True
    assert attempted_redirect.status_code == 422
    assert store.get_agent(agent["id"])["metadata"]["endpoint"] == endpoint


def test_public_route_missing_agent_and_missing_endpoint(monkeypatch):
    store = _store()
    agent = store.register_agent("No endpoint", ["research"], {})
    monkeypatch.setattr(main_mod, "store", store)
    with TestClient(main_mod.app) as client:
        missing = client.post("/agents/agent_missing/endpoint/refresh")
        undeclared = client.post(f"/agents/{agent['id']}/endpoint/refresh")
    assert missing.status_code == 404
    assert undeclared.status_code == 422
    assert "no declared endpoint" in undeclared.text
