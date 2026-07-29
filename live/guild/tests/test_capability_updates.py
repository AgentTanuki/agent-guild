"""Authenticated capability updates preserve one identity and one audit trail."""
import os

os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.state import store  # noqa: E402


client = TestClient(app)


def _register(name: str, capabilities: list[str]) -> dict:
    response = client.post(
        "/agents/register",
        json={"name": name, "capabilities": capabilities},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_capability_update_requires_the_agents_key():
    agent = _register("capability-auth", ["fact-check"])
    path = f"/agents/{agent['id']}/capabilities"

    assert client.post(path, json={"capabilities": ["coding"]}).status_code == 401
    assert client.post(
        path,
        json={"capabilities": ["coding"]},
        headers={"X-API-Key": "sk_wrong"},
    ).status_code == 401

    profile = client.get(f"/agents/{agent['id']}").json()
    assert profile["capabilities"] == ["fact-check"]


def test_capability_update_changes_live_supply_without_replacing_identity():
    agent = _register("capability-demand", ["fact-check", "code-review", "research"])
    path = f"/agents/{agent['id']}/capabilities"
    capabilities = [
        "fact-check",
        "code-review",
        "research",
        "coding",
        "web-research",
        "code_review",
    ]

    response = client.post(
        path,
        json={"capabilities": capabilities},
        headers={"X-API-Key": agent["api_key"]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent_id"] == agent["id"]
    assert body["capabilities"] == capabilities
    assert body["added"] == ["coding", "web-research", "code_review"]
    assert body["removed"] == []
    assert body["changed"] is True
    assert body["guild_next"]["primary"]

    profile = client.get(f"/agents/{agent['id']}").json()
    assert profile["id"] == agent["id"]
    assert profile["did"] == agent["did"]
    assert profile["capabilities"] == capabilities
    supply = store.capability_index()
    assert supply["coding"] >= 1
    assert supply["web-research"] >= 1
    assert supply["code_review"] >= 1

    event = next(
        entry
        for entry in reversed(store.ledger_records)
        if entry.get("type") == "capability_change"
        and entry.get("body", {}).get("agent_id") == agent["id"]
    )
    assert event["actor_did"] == agent["did"]
    assert event["body"]["added"] == ["coding", "web-research", "code_review"]
    assert "api_key" not in str(event)


def test_capability_update_is_idempotent_and_can_retire_supply():
    agent = _register("capability-retire", ["research"])
    path = f"/agents/{agent['id']}/capabilities"
    headers = {"X-API-Key": agent["api_key"]}

    same = client.post(path, json={"capabilities": ["research", "research"]},
                       headers=headers)
    assert same.status_code == 200, same.text
    assert same.json()["changed"] is False
    assert same.json()["capabilities"] == ["research"]

    retired = client.post(path, json={"capabilities": []}, headers=headers)
    assert retired.status_code == 200, retired.text
    assert retired.json()["changed"] is True
    assert retired.json()["removed"] == ["research"]
    assert client.get(f"/agents/{agent['id']}").json()["capabilities"] == []


def test_capability_update_rejects_blank_or_oversized_names():
    agent = _register("capability-validation", ["research"])
    path = f"/agents/{agent['id']}/capabilities"
    headers = {"X-API-Key": agent["api_key"]}

    blank = client.post(path, json={"capabilities": ["  "]}, headers=headers)
    assert blank.status_code == 400
    assert "blank" in blank.json()["detail"]

    too_long = client.post(
        path,
        json={"capabilities": ["x" * 129]},
        headers=headers,
    )
    assert too_long.status_code == 400
    assert "128" in too_long.json()["detail"]
