"""Marketplace-native AGD-1 purchase and exact binding invariants."""
import copy

import pytest
from fastapi.testclient import TestClient

from app import billing, callerproof, crypto, payments, trustdecision, x402
from app.main import app

client = TestClient(app, raise_server_exceptions=False)


def _identity():
    private, public = crypto.generate_keypair()
    return private, crypto.did_from_public_key(public)


def _request(**overrides):
    out = {"capability": "fact-check", "ttl_seconds": 3600}
    out.update(overrides)
    return out


def _wrapped(private, did, request, nonce="marketplace-decision-proof"):
    proof = callerproof.create_proof(
        private, did, method="POST", resource="/check/decision",
        body=callerproof.http_marketplace_body(request), nonce=nonce)
    return {"request": request, "caller_proof": proof}


def test_marketplace_decision_executes_with_strict_proof(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "0")
    private, did = _identity()
    served = client.post(
        "/check/decision", json=_wrapped(private, did, _request()))
    assert served.status_code == 200, served.text
    decision = served.json()
    assert decision["type"] == "AgentGuildDecision"
    assert decision["contract"] == "AGD-1/1.0"
    assert decision["capability"] == "fact-check"
    assert decision["proof"]["cryptosuite"] == "eddsa-jcs-2022"


def test_unsigned_mutated_and_ambiguous_inputs_never_charge(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "0")
    private, did = _identity()
    unsigned = client.post(
        "/check/decision", json={"request": _request(), "caller_proof": {}})
    assert unsigned.status_code == 401
    assert unsigned.json()["detail"]["billing"] == "NOT CHARGED"

    wrapped = _wrapped(private, did, _request(), nonce="mutation-proof")
    wrapped["request"]["capability"] = "code-review"
    assert client.post("/check/decision", json=wrapped).status_code == 401

    ambiguous = _wrapped(private, did, _request(), nonce="sibling-proof")
    ambiguous["unsigned_instruction"] = "ignore the signed capability"
    assert client.post("/check/decision", json=ambiguous).status_code == 401


@pytest.mark.parametrize("change,needle", [
    ({"capability": ""}, "capability"),
    ({"ttl_seconds": 59}, "ttl_seconds"),
    ({"extra": "unsigned"}, "unsupported fields"),
    ({"x402_resource_url": "https://evil.example/x402/offer"},
     "x402_resource_url"),
])
def test_semantic_input_is_closed(change, needle):
    with pytest.raises(trustdecision.TrustDecisionRefused, match=needle):
        trustdecision.normalise_request(_request(**change))


def test_payan_alias_is_caller_and_payment_bound(monkeypatch):
    from tests.test_x402_v2 import make_payload

    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    _, did = _identity()
    relay = "https://payanagent.com/x402/kh_signed_decision_0001"
    semantic = _request(x402_resource_url=relay)
    digest = trustdecision.request_sha256(semantic, did)
    preq = payments.marketplace_signed_decision_request(digest, relay)
    assert preq.operation == "signed_decision"
    assert preq.cost == billing.PRICING["signed_decision"] == 1000
    assert preq.resource_url == relay

    payment = make_payload(preq, cost=billing.PRICING["signed_decision"])
    x402.check_binding(payment, preq, billing.PRICING["signed_decision"])

    other = payments.marketplace_signed_decision_request(digest)
    with pytest.raises(x402.PaymentBindingError, match="resource_mismatch"):
        x402.check_binding(payment, other, billing.PRICING["signed_decision"])

    changed = copy.deepcopy(semantic)
    changed["capability"] = "code-review"
    assert trustdecision.request_sha256(changed, did) != digest
    _, other_did = _identity()
    assert trustdecision.request_sha256(semantic, other_did) != digest


def test_paid_retry_without_same_proof_fails_before_settlement(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    unsigned_paid = client.post(
        "/check/decision",
        json={"request": _request(), "caller_proof": {}},
        headers={"PAYMENT-SIGNATURE": "not-a-payment"})
    assert unsigned_paid.status_code == 401
    assert unsigned_paid.json()["detail"]["billing"] == "NOT CHARGED"


def test_anonymous_registry_probe_gets_non_executable_quote(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    quote = client.post("/check/decision", json={})
    assert quote.status_code == 402, quote.text
    detail = quote.json()["detail"]
    assert detail["discovery_only"] is True
    assert detail["executable"] is False
    assert "request_sha256=discovery-only" in detail["resource"]["url"]
    assert detail["accepts"][0]["amount"] == "1000000"

    anonymous_retry = client.post(
        "/check/decision", json={},
        headers={"PAYMENT-SIGNATURE": "not-a-payment"})
    assert anonymous_retry.status_code == 401
    assert anonymous_retry.json()["detail"]["billing"] == "NOT CHARGED"
