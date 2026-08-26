"""Signed, privacy-preserving receipts for free preflight runs."""
import json
import re

from fastapi.testclient import TestClient

from app import main
from app import preflightreceipt
from app.main import app
from app.mcp_server import guild_preflight
from app.state import store
from app.store import Store


client = TestClient(app)


def _result(url: str, **_kwargs):
    return {
        "target": url,
        "verdict": "delegate_with_caution",
        "checks": [{"name": "agent_card_resolves", "status": "proven"}],
        "failed": [],
        "unknowns": ["independent_evidence"],
    }


def _assert_receipt_shape(payload: dict, transport: str) -> dict:
    receipt = payload["receipt"]
    assert receipt["type"] == "AgentGuildPreflightReceipt"
    assert receipt["contract"] == "AGPF-1/1.0"
    assert receipt["transport"] == transport
    assert re.fullmatch(r"pfobs_[A-Za-z0-9_-]{40,50}",
                        receipt["observation_id"])
    assert receipt["observation_commitment"].startswith("sha256:")
    assert receipt["target_commitment"].startswith("sha256:")
    assert receipt["result_commitment"].startswith("sha256:")
    assert receipt["proof"]["cryptosuite"] == "eddsa-jcs-2022"
    assert receipt["proof"]["proofValue"].startswith("z")
    return receipt


def test_http_receipt_is_signed_private_and_exactly_verifiable(monkeypatch):
    monkeypatch.setattr(main.preflight, "run", _result)
    target = "https://counterparty.example/a2a?nonce=private-invitation"

    response = client.get("/preflight", params={"url": target}, headers={
        "User-Agent": "receipt-test-machine/1.0",
    })

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    receipt = _assert_receipt_shape(payload, "http")
    result = {key: value for key, value in payload.items() if key != "receipt"}

    raw = next(e for e in reversed(store.events)
               if e.get("observation_commitment")
               == receipt["observation_commitment"])
    assert raw["type"] == "preflight_run"
    assert raw["key"] != "anon"
    assert raw["actor_distinct"] is True
    assert "observation_id" not in raw
    assert "target" not in raw
    assert raw["target_commitment"] == receipt["target_commitment"]
    assert raw["result_commitment"] == receipt["result_commitment"]

    public = client.get("/instrumentation/recent", params={"limit": 20}).json()
    event = next(e for e in public["events"]
                 if e.get("observation_commitment")
                 == receipt["observation_commitment"])
    assert event["type"] == "preflight_run"
    assert event["user_agent"] == "receipt-test-machine/1.0"
    assert "observation_id" not in event
    assert "target" not in event

    verified = client.post("/preflight/receipt/verify", json={
        "receipt": receipt, "target": target, "result": result,
    }).json()
    assert verified["valid"] is True
    assert verified["signature_valid"] is True
    assert verified["schema_valid"] is True
    assert verified["issuer_trusted"] is True
    assert verified["observation_commitment_valid"] is True
    assert verified["exact_target_match"] is True
    assert verified["exact_result_match"] is True

    wrong_target = client.post("/preflight/receipt/verify", json={
        "receipt": receipt, "target": "https://other.example/a2a",
    }).json()
    assert wrong_target["valid"] is False
    assert wrong_target["exact_target_match"] is False


def test_receipt_is_unique_and_cannot_be_caller_selected(monkeypatch):
    monkeypatch.setattr(main.preflight, "run", _result)
    supplied = "pfobs_caller_selected"

    first = client.get("/preflight", params={
        "url": "https://one.example/a2a",
        "observation_id": supplied,
    }).json()["receipt"]["observation_id"]
    second = client.get("/preflight", params={
        "url": "https://two.example/a2a",
    }).json()["receipt"]["observation_id"]

    assert first != supplied
    assert second != supplied
    assert first != second


def test_tampered_receipt_or_result_fails(monkeypatch):
    monkeypatch.setattr(main.preflight, "run", _result)
    payload = client.get("/preflight", params={
        "url": "https://one.example/a2a",
    }).json()
    receipt = payload["receipt"]
    result = {key: value for key, value in payload.items() if key != "receipt"}

    tampered_receipt = {**receipt, "transport": "a2a"}
    assert client.post("/preflight/receipt/verify",
                       json={"receipt": tampered_receipt}).json()["valid"] is False
    tampered_result = {**result, "verdict": "delegate"}
    out = client.post("/preflight/receipt/verify", json={
        "receipt": receipt, "result": tampered_result,
    }).json()
    assert out["valid"] is False
    assert out["exact_result_match"] is False

    extra_field_result = {**result,
                          "observation_id": "caller-added-and-ignored"}
    out = client.post("/preflight/receipt/verify", json={
        "receipt": receipt, "result": extra_field_result,
    }).json()
    assert out["valid"] is False
    assert out["exact_result_match"] is False


def test_self_signed_lookalike_from_an_untrusted_issuer_fails(monkeypatch):
    monkeypatch.setattr(main.preflight, "run", _result)
    other_store = Store()
    forged = preflightreceipt.issue(
        other_store, target="https://one.example/a2a",
        result=_result("https://one.example/a2a"), transport="http")
    out = client.post("/preflight/receipt/verify",
                      json={"receipt": forged}).json()
    assert out["valid"] is False
    assert out["issuer_trusted"] is False
    assert out["signature_valid"] is False


def test_malformed_issuer_and_contract_return_invalid_not_500(monkeypatch):
    monkeypatch.setattr(main.preflight, "run", _result)
    receipt = client.get("/preflight", params={
        "url": "https://one.example/a2a",
    }).json()["receipt"]
    malformed = {**receipt, "issuer": "did:key:z"}
    malformed["proof"] = {
        **receipt["proof"], "verificationMethod": "did:key:z#z"}
    response = client.post("/preflight/receipt/verify",
                           json={"receipt": malformed})
    assert response.status_code == 200
    assert response.json()["valid"] is False

    wrong_purpose = {**receipt, "proof": {
        **receipt["proof"], "proofPurpose": "authentication",
    }}
    out = client.post("/preflight/receipt/verify",
                      json={"receipt": wrong_purpose}).json()
    assert out["valid"] is False
    assert out["schema_valid"] is False


def test_mcp_and_a2a_issue_the_same_receipt_contract(monkeypatch):
    monkeypatch.setattr(main.preflight, "run", _result)

    mcp_payload = guild_preflight("https://mcp.example/a2a", ctx=None)
    mcp_receipt = _assert_receipt_shape(mcp_payload, "mcp")
    mcp_event = next(e for e in reversed(store.events)
                     if e.get("observation_commitment")
                     == mcp_receipt["observation_commitment"])
    assert mcp_event["transport"] == "mcp"
    assert mcp_event["actor_distinct"] is False

    response = client.post("/a2a", json={
        "jsonrpc": "2.0", "id": "receipt", "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{
            "kind": "text", "text": "preflight: https://a2a.example/a2a",
        }]}},
    })
    assert response.status_code == 200
    a2a_payload = json.loads(response.json()["result"]["parts"][0]["text"])
    a2a_receipt = _assert_receipt_shape(a2a_payload, "a2a")
    a2a_result = {key: value for key, value in a2a_payload.items()
                  if key != "receipt"}
    a2a_verified = client.post("/preflight/receipt/verify", json={
        "receipt": a2a_receipt,
        "target": "https://a2a.example/a2a",
        "result": a2a_result,
    }).json()
    assert a2a_verified["valid"] is True
    assert a2a_verified["exact_result_match"] is True
    a2a_event = next(e for e in reversed(store.events)
                     if e.get("observation_commitment")
                     == a2a_receipt["observation_commitment"])
    assert a2a_event["transport"] == "a2a"
    assert "observation_id" not in a2a_event
    assert "target" not in a2a_event


def test_recent_feed_omits_receipt_fields_for_legacy_events():
    store.record_event(None, "query", ua="legacy-machine/1.0",
                       endpoint="best_agent")
    event = store.recent_events(limit=1)[0]
    assert "observation_id" not in event
    assert "observation_commitment" not in event
    assert "target_commitment" not in event
    assert "result_commitment" not in event


def test_llms_contract_explains_signed_receipt_limits():
    body = client.get("/llms.txt").text
    assert "signed AGPF-1 receipt" in body
    assert "POST /preflight/receipt/verify" in body
    assert "does not prove caller identity or authority" in body
