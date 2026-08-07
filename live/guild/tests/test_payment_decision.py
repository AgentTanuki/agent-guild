"""Exact-payment decision: signature, binding, policy and billing invariants."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from app import crypto, paymentdecision, payments, pricing, walletbinding
from app.store import Store

NETWORK = "eip155:8453"
ASSET = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


def _request(target_wallet: str, **overrides):
    out = {
        "payment": {
            "scheme": "exact",
            "network": NETWORK,
            "asset": ASSET,
            "amount": "25000",
            "pay_to": target_wallet,
            "resource": "https://seller.example/research/42",
        },
        "capability": "fact-check",
        "policy": {"max_risk": 40, "min_confidence": 0.7},
        "ttl_seconds": 300,
    }
    for key, value in overrides.items():
        if key in out["payment"]:
            out["payment"][key] = value
        else:
            out[key] = value
    return out


def _bound_store(monkeypatch):
    local = Store(path="")
    _private, public = crypto.generate_keypair()
    agent = local.register_agent(
        "payment-provider", ["fact-check"], {}, public_key=public)
    wallet = Account.create()
    walletbinding.issue_credential(
        local, did=agent["did"], address=wallet.address, network=NETWORK,
        challenge_nonce="payment-decision-test")
    risk = {
        "schema_version": 2,
        "agent_id": agent["id"],
        "estimate": 0.92,
        "confidence": 0.91,
        "staleness": None,
        "explanation": ["test evidence"],
        "collusion_suspicion": 0.02,
        "risk": 10.0,
        "recommendation": "hire",
        "trust": 92.0,
        "deprecated": ["risk", "recommendation", "trust"],
    }
    monkeypatch.setattr(local, "risk_for", lambda agent_id: risk)
    return local, agent, wallet, risk


def test_signed_decision_binds_exact_transaction_and_cannot_be_weakened(
        monkeypatch):
    local, agent, wallet, risk = _bound_store(monkeypatch)
    request = _request(
        wallet.address,
        policy={"max_risk": 99, "min_confidence": 0.0})
    issued = datetime(2026, 8, 7, 18, 0, tzinfo=timezone.utc)

    decision = paymentdecision.issue(local, request, now=issued)
    subject = decision["credentialSubject"]

    assert decision["type"] == [
        "VerifiableCredential", "AgentGuildPaymentDecision"]
    assert decision["proof"]["cryptosuite"] == "eddsa-jcs-2022"
    assert subject["contract"] == "AGPD-1/1.0"
    assert subject["decision"] == "allow"
    assert subject["counterparty"]["agent"]["id"] == agent["id"]
    assert subject["counterparty"]["wallet_binding"]["status"][
        "status"] == "active"
    assert subject["risk"] == risk
    assert subject["provenance"]["anchoring"] == "checkpoint_committed_only"
    assert subject["checkpoint"]["head_hash"] == \
        subject["provenance"]["checkpoint"]["head_hash"]
    assert subject["payment"] == paymentdecision.normalise_request(
        request)["payment"]
    assert subject["policy"]["requested"] == {
        "max_risk": 99.0, "min_confidence": 0.0}
    assert subject["policy"]["effective"] == {
        "max_risk": paymentdecision.SERVER_MAX_RISK,
        "min_confidence": paymentdecision.SERVER_MIN_CONFIDENCE}
    assert paymentdecision.verify(
        decision, expected_request=request, now=issued)["valid"] is True

    changed = copy.deepcopy(request)
    changed["payment"]["amount"] = "25001"
    assert paymentdecision.verify(
        decision, expected_request=changed, now=issued)["valid"] is False
    tampered = copy.deepcopy(decision)
    tampered["credentialSubject"]["payment"]["pay_to"] = Account.create().address.lower()
    assert paymentdecision.verify(tampered, now=issued)["valid"] is False


@pytest.mark.parametrize("field,value", [
    ("amount", "25001"),
    ("asset", "0x" + "11" * 20),
    ("pay_to", "0x" + "22" * 20),
    ("network", "eip155:84532"),
    ("resource", "https://seller.example/research/43"),
    ("scheme", "upto"),
])
def test_every_selected_payment_field_changes_settlement_binding(field, value):
    request = _request("0x" + "33" * 20)
    changed = copy.deepcopy(request)
    changed["payment"][field] = value
    assert paymentdecision.request_sha256(changed) != \
        paymentdecision.request_sha256(request)
    assert payments.payment_decision_request(
        paymentdecision.request_sha256(changed)).resource_url != \
        payments.payment_decision_request(
            paymentdecision.request_sha256(request)).resource_url


def test_unknown_wallet_is_a_signed_block_not_misconduct():
    local = Store(path="")
    request = _request("0x" + "44" * 20)
    now = datetime(2026, 8, 7, 18, 0, tzinfo=timezone.utc)
    decision = paymentdecision.issue(local, request, now=now)
    subject = decision["credentialSubject"]

    assert subject["decision"] == "block"
    assert "exact_wallet_not_bound_to_registered_agent" in subject["failures"]
    assert subject["counterparty"]["resolution_status"] == "unbound"
    assert subject["counterparty"]["agent"] is None
    assert "misconduct" not in subject["reason"]
    assert paymentdecision.verify(decision, now=now)["valid"] is True


@pytest.mark.parametrize("change,needle", [
    ({"amount": "0"}, "amount"),
    ({"amount": "1.5"}, "amount"),
    ({"amount": str(2 ** 256)}, "amount"),
    ({"pay_to": "not-an-address"}, "pay_to"),
    ({"asset": "USDC"}, "asset"),
    ({"resource": "file:///etc/passwd"}, "resource"),
    ({"resource": "https://user:pass@example.com/x"}, "resource"),
    ({"network": "solana:mainnet"}, "network"),
])
def test_malformed_or_unsupported_payment_fails_closed(change, needle):
    with pytest.raises(paymentdecision.PaymentDecisionRefused, match=needle):
        paymentdecision.normalise_request(
            _request("0x" + "55" * 20, **change))


def test_malformed_ttl_fails_closed():
    with pytest.raises(paymentdecision.PaymentDecisionRefused,
                       match="ttl_seconds"):
        paymentdecision.normalise_request(
            _request("0x" + "55" * 20, ttl_seconds="not-an-integer"))


def test_http_route_issues_then_meters_and_verification_is_free(
        monkeypatch):
    from app import main

    local, _agent, wallet, _risk = _bound_store(monkeypatch)
    monkeypatch.setattr(main, "store", local)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "0")
    request = _request(wallet.address)

    with TestClient(main.app) as client:
        issued = client.post("/wallet-binding/decision", json=request)
        assert issued.status_code == 200, issued.text
        credential = issued.json()
        assert credential["credentialSubject"]["decision"] == "allow"
        verified = client.post(
            "/wallet-binding/decision/verify",
            json={"decision": credential, "request": request})
        assert verified.status_code == 200
        assert verified.json()["valid"] is True

    event = [e for e in local.events
             if e.get("type") == "payment_decision_issued"][-1]
    assert event["request_sha256"] == paymentdecision.request_sha256(request)
    assert event["price_credits"] == pricing.price("payment_decision") == 10
    assert event["settlement_mode"] == "free"


def test_invalid_http_request_is_never_metered(monkeypatch):
    from app import main

    local = Store(path="")
    monkeypatch.setattr(main, "store", local)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    bad = _request("not-a-wallet")
    with TestClient(main.app) as client:
        response = client.post("/wallet-binding/decision", json=bad)
    assert response.status_code == 422
    assert response.json()["detail"]["billing"].startswith("NOT CHARGED")
    assert not [e for e in local.events
                if e.get("type") == "payment_decision_issued"]
