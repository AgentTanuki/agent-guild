"""Machine-native passport identity resolution and recovery guidance."""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import mcp_server
from app.main import app
from app.store import Store


def _tool_fn(tool):
    return getattr(tool, "fn", tool)


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(path=str(tmp_path / "guild.json"))


def test_store_issues_passport_for_registered_did(store):
    registered = store.register_agent(
        name="did-passport-subject", capabilities=["research"], metadata={}
    )

    passport = store.issue_passport(registered["did"])

    assert passport is not None
    assert passport["credentialSubject"]["id"] == registered["did"]
    issued = [e for e in store.events if e["type"] == "passport_issued"]
    assert len(issued) == 1
    assert issued[0]["subject_id"] == registered["id"]


def test_mcp_passport_accepts_registered_did(store, monkeypatch):
    monkeypatch.setattr(mcp_server, "store", store)
    registered = store.register_agent(
        name="mcp-did-passport-subject", capabilities=["code-review"], metadata={}
    )

    passport = _tool_fn(mcp_server.guild_passport)(
        agent_id=registered["did"], ctx=None
    )

    assert passport["credentialSubject"]["id"] == registered["did"]


def test_mcp_unknown_identity_returns_executable_recovery(store, monkeypatch):
    monkeypatch.setattr(mcp_server, "store", store)

    result = _tool_fn(mcp_server.guild_passport)(
        agent_id="did:key:z6MkUnknownExternalAgent", ctx=None
    )

    assert result["error"] == "agent is not registered on Agent Guild"
    assert result["agent_ref"] == "did:key:z6MkUnknownExternalAgent"
    assert "guild_register" in result["next_step"]
    assert "passport_offer:mcp" in result["next_step"]
    assert not [e for e in store.events if e["type"] == "passport_issued"]


def test_http_passport_accepts_registered_did(store, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "store", store)
    client = TestClient(app)
    registered = store.register_agent(
        name="http-did-passport-subject", capabilities=["fact-check"], metadata={}
    )

    response = client.get(f"/agents/{registered['did']}/passport")

    assert response.status_code == 200
    assert response.json()["credentialSubject"]["id"] == registered["did"]
    assert response.headers["X-Guild-Journey"].endswith(
        f"/agents/{registered['id']}/journey"
    )
