"""In-band identity conversion without beacons or census-rule changes.

Pins the 2026-08-16 conversion experiment: registration returns an immediately
completable proof challenge, existing authenticated members receive a bounded
next-call hint, successful proofs retain the served source tag, and public
machine-readable surfaces explain honest runtime identification.
"""
from __future__ import annotations

import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402

from app import journey, proving  # noqa: E402
from app.crypto import generate_keypair, sign_payload  # noqa: E402
from app.main import app  # noqa: E402
from app.state import store  # noqa: E402


client = TestClient(app)


def test_registration_challenge_survives_hashed_at_rest_and_converts(monkeypatch):
    """Production hashes API keys, so the challenge must land on the stored
    record rather than only on the one-time response copy."""
    monkeypatch.setenv("GUILD_HASH_KEYS", "1")
    response = client.post(
        "/agents/register",
        json={"name": "Identity-Hashed", "capabilities": ["fact-check"]},
        headers={"User-Agent": "crewai/9.1"},
    )
    assert response.status_code == 200, response.text
    reg = response.json()
    agent = store.get_agent(reg["id"])

    assert reg["api_key"].startswith("sk_")
    assert agent["api_key"] is None
    assert agent["proving_challenge"]["nonce"] == \
        reg["proof_challenge"]["challenge"]["guild_proving_challenge"]
    assert reg["guild_next"]["primary"]["action"] == "complete_key_proof"
    assert reg["guild_next"]["guild_prove_hint"]["next_call"]["url"].endswith(
        f"/agents/{reg['id']}/prove/verify?hint=register-v1")

    headers = {"X-API-Key": reg["api_key"], "User-Agent": "crewai/9.1"}
    return_visit = client.get(f"/agents/{reg['id']}/journey", headers=headers)
    assert return_visit.status_code == 200
    assert return_visit.headers["X-Agent-Guild-Prove"].endswith(
        f"/agents/{reg['id']}/prove/verify?hint=inband-v1")
    assert return_visit.headers["X-Agent-Guild-Prove-Hint"] == \
        proving.PROVE_HINT_VERSION

    verified = client.post(
        f"/agents/{reg['id']}/prove/verify?hint=register-v1",
        headers=headers,
        json={},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["status"] == "proven"
    assert "X-Agent-Guild-Prove" not in verified.headers

    completed = [
        event for event in store.events
        if event.get("type") == "prove_completed"
        and event.get("agent_id") == reg["id"]
    ]
    assert completed[-1]["prove_hint"] == "register-v1"


def test_existing_authenticated_member_gets_inband_recovery_path():
    agent = store.register_agent("Identity-Legacy", ["fact-check"], {})
    headers = {"X-API-Key": agent["api_key"], "User-Agent": "autogen/4.2"}

    first_return = client.get(
        f"/agents/{agent['id']}/journey", headers=headers)
    assert first_return.status_code == 200
    assert first_return.json()["next_actions"][0]["action"] == \
        "prove_key_control"
    assert first_return.headers["X-Agent-Guild-Prove"].endswith(
        f"/agents/{agent['id']}/prove")

    started = client.post(f"/agents/{agent['id']}/prove", headers=headers)
    assert started.status_code == 200
    assert started.headers["X-Agent-Guild-Prove"].endswith(
        f"/agents/{agent['id']}/prove/verify?hint=inband-v1")

    verified = client.post(
        f"/agents/{agent['id']}/prove/verify?hint=inband-v1",
        headers=headers,
        json={},
    )
    assert verified.status_code == 200
    completed = [
        event for event in store.events
        if event.get("type") == "prove_completed"
        and event.get("agent_id") == agent["id"]
    ]
    assert completed[-1]["prove_hint"] == "inband-v1"


def test_self_sovereign_registration_challenge_verifies_directly():
    private_key, public_key = generate_keypair()
    response = client.post("/agents/register", json={
        "name": "Identity-Sovereign",
        "capabilities": ["fact-check"],
        "public_key": public_key,
    })
    assert response.status_code == 200
    reg = response.json()
    challenge = reg["proof_challenge"]["challenge"]
    signature = sign_payload(challenge, private_key)

    verified = client.post(
        f"/agents/{reg['id']}/prove/verify?hint=register-v1",
        json={"signature": signature},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["proof_of_conduct"]["proof_class"] == "key_control"


def test_expired_or_malformed_challenge_never_points_to_dead_verify_call():
    agent = store.register_agent("Identity-Expired", ["fact-check"], {})
    proving.issue_challenge(store, agent)
    agent["proving_challenge"]["expires_at"] = "not-a-date"
    action = journey.next_actions(store, agent)[0]
    assert action["action"] == "prove_key_control"
    assert action["call"].endswith(f"/agents/{agent['id']}/prove")


def test_hint_source_is_closed_and_discovery_surfaces_explain_identity():
    agent = store.register_agent("Identity-Hint-Enum", ["fact-check"], {})
    proving.issue_challenge(store, agent)
    invalid = client.post(
        f"/agents/{agent['id']}/prove/verify?hint=made-up-campaign",
        headers={"X-API-Key": agent["api_key"]},
        json={},
    )
    assert invalid.status_code == 422

    llms = client.get("/llms.txt").text
    assert "Identify your runtime honestly" in llms
    assert "clientInfo.name + clientInfo.version" in llms

    card = client.get("/.well-known/agent-card.json").json()
    identity_extension = next(
        extension for extension in card["capabilities"]["extensions"]
        if extension["uri"].endswith("/ext/caller-identity/v1")
    )
    assert identity_extension["required"] is False
    assert "actual framework" in identity_extension["params"]["http_header"]

    catalog = client.get("/.well-known/ai-catalog.json").json()
    for entry in catalog["entries"]:
        assert "callerIdentity" in entry["metadata"]
