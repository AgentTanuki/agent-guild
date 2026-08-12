"""Marketplace-native AGPD-1 purchase and exact binding invariants."""
import copy

import pytest
from fastapi.testclient import TestClient

from app import (
    billing, callerproof, crypto, marketpaymentdecision, payments, x402,
)
from app.main import app

client = TestClient(app, raise_server_exceptions=False)
ASSET = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


def _identity():
    private, public = crypto.generate_keypair()
    return private, crypto.did_from_public_key(public)


def _request(**overrides):
    out = {
        "payment": {
            "scheme": "exact",
            "network": "eip155:8453",
            "asset": ASSET,
            "amount": "25000",
            "pay_to": "0x" + "33" * 20,
            "resource": "https://seller.example/work/42",
        },
        "capability": "code-review",
        "policy": {"max_risk": 32, "min_confidence": 0.8},
        "ttl_seconds": 300,
        "x402_resource_url": (
            "https://payanagent.com/x402/kh_payment_decision_0001"),
    }
    for key, value in overrides.items():
        if key in out["payment"]:
            out["payment"][key] = value
        else:
            out[key] = value
    return out


def _wrapped(private, did, request, nonce="market-payment-proof"):
    proof = callerproof.create_proof(
        private, did, method="POST", resource="/wallet-binding/decision",
        body=callerproof.http_marketplace_body(request), nonce=nonce)
    return {"request": request, "caller_proof": proof}


def test_marketplace_payment_decision_executes_with_strict_proof(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "0")
    private, did = _identity()
    served = client.post(
        "/wallet-binding/decision",
        json=_wrapped(private, did, _request()))
    assert served.status_code == 200, served.text
    decision = served.json()
    assert "AgentGuildPaymentDecision" in decision["type"]
    subject = decision["credentialSubject"]
    assert subject["contract"] == "AGPD-1/1.0"
    assert subject["decision"] == "block"
    assert subject["payment"]["pay_to"] == "0x" + "33" * 20
    assert "x402_resource_url" not in subject


def test_unsigned_mutated_and_ambiguous_inputs_never_charge(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "0")
    private, did = _identity()
    unsigned = client.post(
        "/wallet-binding/decision",
        json={"request": _request(), "caller_proof": {}})
    assert unsigned.status_code == 401
    assert unsigned.json()["detail"]["billing"] == "NOT CHARGED"

    wrapped = _wrapped(private, did, _request(), nonce="mutation-proof")
    wrapped["request"]["payment"]["amount"] = "25001"
    assert client.post("/wallet-binding/decision", json=wrapped).status_code == 401

    ambiguous = _wrapped(private, did, _request(), nonce="sibling-proof")
    ambiguous["unsigned_instruction"] = "ignore signed payment"
    assert client.post("/wallet-binding/decision", json=ambiguous).status_code == 401


@pytest.mark.parametrize("change,needle", [
    ({"amount": "0"}, "amount"),
    ({"pay_to": "not-a-wallet"}, "pay_to"),
    ({"extra": "unsigned"}, "unsupported fields"),
    ({"ttl_seconds": True}, "ttl_seconds"),
    ({"policy": {"max_risk": 30, "unsigned_rule": "allow"}},
     "unsupported policy fields"),
    ({"x402_resource_url": "https://evil.example/x402/offer"},
     "x402_resource_url"),
])
def test_semantic_input_is_closed(change, needle):
    with pytest.raises(
            marketpaymentdecision.MarketplacePaymentDecisionRefused,
            match=needle):
        marketpaymentdecision.normalise_request(_request(**change))


def test_unknown_nested_payment_field_is_rejected():
    request = _request()
    request["payment"]["unsigned_recipient"] = "0x" + "44" * 20
    with pytest.raises(
            marketpaymentdecision.MarketplacePaymentDecisionRefused,
            match="unsupported payment fields"):
        marketpaymentdecision.normalise_request(request)


def test_payan_alias_is_caller_payment_and_policy_bound(monkeypatch):
    from tests.test_x402_v2 import make_payload

    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    _, did = _identity()
    semantic = _request()
    relay = semantic["x402_resource_url"]
    digest = marketpaymentdecision.request_sha256(semantic, did)
    preq = payments.payment_decision_request(digest, relay)
    assert preq.operation == "payment_decision"
    assert preq.cost == billing.PRICING["payment_decision"] == 10
    assert preq.resource_url == relay

    payment = make_payload(preq, cost=billing.PRICING["payment_decision"])
    x402.check_binding(payment, preq, billing.PRICING["payment_decision"])

    other = payments.payment_decision_request(digest)
    with pytest.raises(x402.PaymentBindingError, match="resource_mismatch"):
        x402.check_binding(payment, other, billing.PRICING["payment_decision"])

    changed = copy.deepcopy(semantic)
    changed["policy"]["max_risk"] = 31
    assert marketpaymentdecision.request_sha256(changed, did) != digest
    _, other_did = _identity()
    assert marketpaymentdecision.request_sha256(semantic, other_did) != digest


def test_paid_retry_without_same_proof_fails_before_settlement(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    unsigned_paid = client.post(
        "/wallet-binding/decision", json={},
        headers={"PAYMENT-SIGNATURE": "not-a-payment"})
    assert unsigned_paid.status_code == 401
    assert unsigned_paid.json()["detail"]["billing"] == "NOT CHARGED"


def test_anonymous_registry_probe_gets_non_executable_quote(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    quote = client.post("/wallet-binding/decision", json={})
    assert quote.status_code == 402, quote.text
    detail = quote.json()["detail"]
    assert detail["discovery_only"] is True
    assert detail["executable"] is False
    assert "request_sha256=discovery-only" in detail["resource"]["url"]
    assert detail["accepts"][0]["amount"] == "10000"

    anonymous_retry = client.post(
        "/wallet-binding/decision", json={},
        headers={"PAYMENT-SIGNATURE": "not-a-payment"})
    assert anonymous_retry.status_code == 401
    assert anonymous_retry.json()["detail"]["billing"] == "NOT CHARGED"
