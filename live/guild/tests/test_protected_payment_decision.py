"""Value-priced, payer-bound, high-assurance AGPD-1 invariants."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from app import (
    callerproof, crypto, paymentdecision, payments, protecteddecision,
    reachability, walletbinding, x402,
)
from app.store import Store

NETWORK = "eip155:8453"
ASSET = x402.USDC_BY_NETWORK[NETWORK]


def _request(pay_to: str, amount: str = "100000000") -> dict:
    return {
        "payment": {
            "scheme": "exact",
            "network": NETWORK,
            "asset": ASSET,
            "amount": amount,
            "pay_to": pay_to,
            "resource": "https://seller.example/work/42",
        },
        "capability": "code-review",
        "policy": {"max_risk": 30, "min_confidence": 0.7},
        "ttl_seconds": 300,
    }


def _risk(confidence=0.91):
    return {
        "schema_version": 2, "estimate": 0.92, "confidence": confidence,
        "risk": 10.0, "recommendation": "hire", "staleness": {
            "age_days": 1, "basis": "latest evidence"},
        "explanation": ["test evidence"],
    }


def _store(monkeypatch, *, collaborations=5, reachable=True):
    local = Store(path="")
    _private, public = crypto.generate_keypair()
    agent = local.register_agent(
        "protected-provider", ["code-review"],
        {"endpoint": "https://seller.example/a2a"}, public_key=public)
    wallet = Account.create()
    walletbinding.issue_credential(
        local, did=agent["did"], address=wallet.address, network=NETWORK,
        challenge_nonce="protected-decision-test")
    record = reachability.make_record(
        "recently_reachable", "declaration_probe", "protocol_handshake",
        "https://seller.example/a2a", detail="test")
    local.agents[agent["id"]]["reachability"] = (
        record if reachable else None)
    monkeypatch.setattr(local, "risk_for", lambda _agent_id: _risk())
    provenance = {
        "counts": {"guild_mediated": collaborations,
                   "verifiable_outcome": 0,
                   "mutual_attestation": 0,
                   "external_import": 0,
                   "one_party_claim": 0,
                   "first_party_bootstrap": 0},
        "verifiable_collaborations": collaborations,
        "checkpoint": None,
    }
    monkeypatch.setattr(local, "provenance_summary", lambda _agent_id: provenance)
    return local, agent, wallet


@pytest.mark.parametrize("amount,credits,tier", [
    ("1", 10, "micro"),
    ("1000000", 10, "micro"),              # $1 -> one-cent floor
    ("100000000", 250, "medium"),          # $100 -> $0.25
    ("1000000000", 2500, "high"),          # $1,000 -> $2.50
    ("1000000000000", 2_500_000, "high"), # $1m -> $2,500
    ("9999999999999999", 10_000_000, "high"), # $10k ceiling
])
def test_quote_is_integer_exact_bps_clamped_and_value_tiered(
        amount, credits, tier, monkeypatch):
    q = protecteddecision.quote(_request("0x" + "33" * 20, amount))
    assert q["basis_points"] == 25
    assert q["fee_credits"] == credits
    assert q["fee_atomic_usdc"] == str(credits * 1000)
    assert q["protected_value_tier"] == tier


@pytest.mark.parametrize("change,needle", [
    ({"network": "eip155:84532"}, "Base mainnet"),
    ({"asset": "0x" + "22" * 20}, "canonical Base mainnet USDC"),
    ({"scheme": "upto"}, "exact payment scheme"),
])
def test_only_exact_base_mainnet_usdc_is_protected(change, needle):
    request = _request("0x" + "33" * 20)
    request["payment"].update(change)
    with pytest.raises(protecteddecision.ProtectedDecisionRefused, match=needle):
        protecteddecision.quote(request)


def test_request_and_gateway_bind_exact_quote_and_caller():
    caller = callerproof.evm_did("0x" + "44" * 20)
    request = _request("0x" + "33" * 20, "100000000")
    quote = protecteddecision.quote(request)
    digest = protecteddecision.request_sha256(request, caller)
    preq = payments.protected_payment_decision_request(digest, quote)
    assert preq.cost == 250
    assert preq.operation == "protected_payment_decision"
    assert "fee_bps=25" in preq.resource_url
    assert "fee_credits=250" in preq.resource_url
    assert "protected_amount" not in preq.resource_url
    other = callerproof.evm_did("0x" + "45" * 20)
    assert protecteddecision.request_sha256(request, other) != digest


def test_signed_allow_requires_reachability_and_evidence_depth(monkeypatch):
    local, _agent, wallet = _store(monkeypatch)
    request = _request(wallet.address, "1000000000")
    caller = callerproof.evm_did("0x" + "44" * 20)
    now = datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)
    decision = paymentdecision.issue(
        local, request, now=now,
        policy_extension=lambda s, n, r, risk, prov:
            protecteddecision.issue_extension(
                s, n, r, risk, prov, caller_did=caller))
    subject = decision["credentialSubject"]
    assert subject["decision"] == "allow"
    assert subject["protection"]["pricing"]["fee_credits"] == 2500
    assert subject["protection"]["required_value_tier"] == "high"
    assert subject["protection"]["reachability"][
        "recommended_for_routing"] is True
    assert subject["protection"]["service_client"]["caller_did"] == caller
    assert protecteddecision.verify(
        decision, expected_request=request, now=now)["valid"] is True


def test_signed_block_names_missing_value_evidence_and_routing(monkeypatch):
    local, _agent, wallet = _store(
        monkeypatch, collaborations=0, reachable=False)
    request = _request(wallet.address, "1000000000")
    caller = callerproof.evm_did("0x" + "44" * 20)
    decision = paymentdecision.issue(
        local, request,
        policy_extension=lambda s, n, r, risk, prov:
            protecteddecision.issue_extension(
                s, n, r, risk, prov, caller_did=caller))
    subject = decision["credentialSubject"]
    assert subject["decision"] == "block"
    assert "counterparty_endpoint_not_verified_reachable" in subject["failures"]
    assert "evidence_depth_below_protected_value_tier" in subject["failures"]


def _wrapped_evm(private_key: str, request: dict, nonce: str):
    endpoint = "/wallet-binding/protected-decision"
    proof = callerproof.create_evm_proof(
        private_key, method="POST", resource=endpoint,
        body=callerproof.http_marketplace_body(request), nonce=nonce)
    return {"request": request, "caller_proof": proof}, proof["payload"]["did"]


def test_http_quote_is_dynamic_and_unsigned_retry_never_settles(monkeypatch):
    from app import main

    local, _agent, wallet = _store(monkeypatch)
    monkeypatch.setattr(main, "store", local)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_NETWORK", NETWORK)
    monkeypatch.setenv("GUILD_X402_ASSET", ASSET)
    monkeypatch.setenv("GUILD_X402_PAY_TO", x402.MAINNET_TREASURY)
    monkeypatch.setattr(x402, "config_errors", lambda: [])
    request = _request(wallet.address, "100000000")
    wrapped, _did = _wrapped_evm(
        "0x" + "47" * 32, request, "protected-http-quote")
    with TestClient(main.app) as client:
        quote = client.post(
            "/wallet-binding/protected-decision", json=wrapped)
        assert quote.status_code == 402, quote.text
        detail = quote.json()["detail"]
        assert detail["accepts"][0]["amount"] == "250000"
        assert "fee_credits=250" in detail["resource"]["url"]
        anonymous = client.post(
            "/wallet-binding/protected-decision", json={},
            headers={"PAYMENT-SIGNATURE": "not-a-payment"})
        assert anonymous.status_code == 401
        assert anonymous.json()["detail"]["billing"] == "NOT CHARGED"
    assert not [r for r in local.billing_log if r.get("type") == "x402_payment"]


def test_pre_settlement_payer_mismatch_is_rejected(monkeypatch):
    from tests.test_x402_v2 import make_payload

    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_NETWORK", NETWORK)
    monkeypatch.setenv("GUILD_X402_ASSET", ASSET)
    monkeypatch.setenv("GUILD_X402_PAY_TO", x402.MAINNET_TREASURY)
    monkeypatch.setattr(x402, "config_errors", lambda: [])
    request = _request("0x" + "33" * 20, "100000000")
    proof_wallet = Account.create()
    caller_did = callerproof.evm_did(proof_wallet.address)
    quote = protecteddecision.quote(request)
    preq = payments.protected_payment_decision_request(
        protecteddecision.request_sha256(request, caller_did), quote)
    payment = make_payload(preq, cost=preq.cost)
    with pytest.raises(x402.PaymentBindingError, match="caller_payer_mismatch"):
        payments.settle_x402(payment, preq, caller_did=caller_did)
