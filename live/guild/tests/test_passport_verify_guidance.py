"""Execute emitted passport verification instructions against the HTTP handler."""
import json
import socket

import pytest
from fastapi.testclient import TestClient

from app import main
from app.store import Store


@pytest.fixture()
def passport_flow(monkeypatch):
    """Use a fresh local store and the real register/prove/passport handlers."""
    def no_network(*args, **kwargs):
        raise AssertionError("passport guidance tests must not use the network")

    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    store = Store(path="")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "0")
    client = TestClient(main.app)
    reg = client.post("/agents/register", json={
        "name": "PassportGuidanceFixture", "capabilities": ["guidance-test"],
    }).json()
    headers = {"X-API-Key": reg["api_key"]}
    assert client.post(f"/agents/{reg['id']}/prove", headers=headers).status_code == 200
    proved = client.post(f"/agents/{reg['id']}/prove/verify", headers=headers)
    assert proved.status_code == 200
    response = client.get(f"/agents/{reg['id']}/passport", headers=headers)
    assert response.status_code == 200
    yield client, store, reg["id"], response.json(), proved.json()
    client.close()


def _advertised_body(instruction, credential):
    """Fill the advertised placeholder without adding/removing a wrapper."""
    if "/credentials/verify " in instruction:
        instruction = instruction.split("/credentials/verify ", 1)[1]
    for placeholder in ("<passport JSON>", "<the passport JSON you fetched>",
                        "<the passport JSON>"):
        instruction = instruction.replace(placeholder, json.dumps(credential))
    # Inbox guidance continues with a sentence after the request JSON.
    body, _ = json.JSONDecoder().raw_decode(instruction)
    return body


@pytest.mark.parametrize("surface", [
    "check", "capabilities", "manifest", "x402_challenge", "prove_bundle", "inbox",
])
def test_advertised_verification_request_accepts_issued_passport(
        passport_flow, monkeypatch, surface):
    client, store, subject_id, credential, proved = passport_flow
    if surface in ("check", "capabilities", "manifest", "x402_challenge"):
        if surface == "x402_challenge":
            monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
        path = {
            "check": "/check?capability=guidance-test",
            "capabilities": "/capabilities",
            "manifest": "/.well-known/agent-guild.json",
            "x402_challenge": "/check?capability=guidance-test",
        }[surface]
        response = client.get(path)
        assert response.status_code == (402 if surface == "x402_challenge" else 200)
        payload = response.json()
        if surface == "x402_challenge":
            payload = payload["detail"]
        instruction = payload["claim_passport"]["verify"]
    elif surface == "prove_bundle":
        call = proved["passport"]["verify_call"]
        assert call["method"] == "POST"
        assert call["url"].endswith("/credentials/verify")
        instruction = call["body"]
    else:
        messages = proved["guild_next"]["inbox"]["messages"]
        instruction = next(m["body"] for m in messages
                           if m["topic"] == "Claim your Agent Guild passport")

    before = len(store.events)
    response = client.post("/credentials/verify",
                           json=_advertised_body(instruction, credential))
    assert response.status_code == 200
    verified = response.json()
    assert verified["valid"] is True, (surface, instruction, verified)
    assert verified["guild_issued"] is True
    assert verified["subject_did"] == credential["credentialSubject"]["id"]
    assert verified["subject_known_to_guild"] is True
    assert verified["live_reputation"] is not None
    # Verifying another's credential is still one anonymous verification,
    # not another issuance or evidence that the verifier owns its subject.
    events = store.events[before:]
    checks = [e for e in events if e["type"] == "passport_verified"]
    assert len(checks) == 1
    assert checks[0]["subject_id"] == subject_id
    assert checks[0]["key"] == "anon"
    assert not any(e["type"] == "passport_issued" for e in events)


@pytest.mark.parametrize("shape", ["credential", "tampered", "wrapped"])
def test_verification_preserves_credential_body_and_signature_contract(
        passport_flow, shape):
    client, _, _, credential, _ = passport_flow
    if shape == "tampered":
        credential["credentialSubject"]["trust"] = 99.9
    elif shape == "wrapped":
        credential = {"credential": credential}
    response = client.post("/credentials/verify", json=credential)
    assert response.status_code == 200
    assert response.json()["valid"] is (shape == "credential")
