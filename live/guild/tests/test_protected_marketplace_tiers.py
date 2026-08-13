"""Fixed-notional Payan tiers preserve protected-value and proof invariants."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import (
    callerproof, paymentdecision, payments, protecteddecision,
    protectedmarket, x402,
)
from tests.test_protected_payment_decision import ASSET, NETWORK, _store


def _request(pay_to: str, tier_id: str, relay: str) -> dict:
    return {
        "payment": {
            "scheme": "exact",
            "network": NETWORK,
            "asset": ASSET,
            "amount": protectedmarket.TIERS[tier_id],
            "pay_to": pay_to,
            "resource": "https://seller.example/work/42",
        },
        "capability": "code-review",
        "policy": {"max_risk": 30, "min_confidence": 0.7},
        "ttl_seconds": 300,
        "x402_resource_url": relay,
    }


def _wrapped(private_key: str, tier_id: str, semantic: dict, nonce: str):
    proof = callerproof.create_evm_proof(
        private_key, method="POST",
        resource=protectedmarket.tier_path(tier_id),
        body=callerproof.http_marketplace_body(semantic), nonce=nonce)
    return {"request": semantic, "caller_proof": proof}, proof["payload"]["did"]


@pytest.mark.parametrize("tier_id,amount,fee_credits,fee_atomic", [
    ("1000-usdc", "1000000000", 2500, "2500000"),
    ("10000-usdc", "10000000000", 25000, "25000000"),
    ("100000-usdc", "100000000000", 250000, "250000000"),
    ("1000000-usdc", "1000000000000", 2500000, "2500000000"),
    ("4000000-usdc", "4000000000000", 10000000, "10000000000"),
])
def test_tier_catalog_is_exact_ordinary_schedule(
        tier_id, amount, fee_credits, fee_atomic):
    quote = protectedmarket.tier_quote(tier_id)
    assert protectedmarket.TIERS[tier_id] == amount
    assert quote["basis_points"] == protecteddecision.DEFAULT_FEE_BPS == 25
    assert quote["fee_credits"] == fee_credits
    assert quote["fee_atomic_usdc"] == fee_atomic
    assert quote["protected_amount_atomic"] == amount
    assert quote == protecteddecision.quote({
        "payment": {
            "scheme": "exact", "network": NETWORK, "asset": ASSET,
            "amount": amount,
            "pay_to": "0x0000000000000000000000000000000000000001",
            "resource": "https://example.invalid/protected-payment",
        },
    })


def test_tier_catalog_publishes_exact_purchasable_marketplace_bindings():
    rows = {row["tier_id"]: row for row in protectedmarket.catalog()}
    assert set(rows) == set(protectedmarket.TIERS)
    assert set(protectedmarket.PAYAN_TIER_OFFERS) == set(protectedmarket.TIERS)
    for tier_id, offer_id in protectedmarket.PAYAN_TIER_OFFERS.items():
        marketplace = rows[tier_id]["marketplace"]
        buy_url = f"https://payanagent.com/x402/{offer_id}"
        assert marketplace == {
            "provider": "PayanAgent",
            "seller_id": protectedmarket.PAYAN_SELLER_ID,
            "offer_id": offer_id,
            "offer_url": (
                f"https://payanagent.com/marketplace/offers/{offer_id}"),
            "buy_url": buy_url,
            "request_binding": {"x402_resource_url": buy_url},
        }


def test_tier_normalizer_binds_exact_amount_and_canonical_relay():
    relay = "https://payanagent.com/x402/kh_protected_tier_1000"
    request = _request("0x" + "33" * 20, "1000-usdc", relay)
    normalized = protectedmarket.normalise_request(request, "1000-usdc")
    assert normalized["payment"]["amount"] == "1000000000"
    assert normalized["x402_resource_url"] == relay
    request["payment"]["amount"] = "999999999"
    with pytest.raises(
            protectedmarket.ProtectedMarketplaceRefused,
            match="must equal the 1000-usdc tier"):
        protectedmarket.normalise_request(request, "1000-usdc")
    request["payment"]["amount"] = "1000000000"
    request["x402_resource_url"] = "https://attacker.example/x402/offer"
    with pytest.raises(
            protectedmarket.ProtectedMarketplaceRefused,
            match="canonical https://payanagent.com"):
        protectedmarket.normalise_request(request, "1000-usdc")


def test_digest_binds_tier_relay_request_and_caller():
    request = _request(
        "0x" + "33" * 20, "1000-usdc",
        "https://payanagent.com/x402/kh_protected_tier_1000")
    caller = callerproof.evm_did("0x" + "44" * 20)
    digest = protectedmarket.request_sha256(request, caller, "1000-usdc")
    changed = {**request, "x402_resource_url":
               "https://payanagent.com/x402/kh_protected_tier_other"}
    assert protectedmarket.request_sha256(
        changed, caller, "1000-usdc") != digest
    assert protectedmarket.request_sha256(
        request, callerproof.evm_did("0x" + "45" * 20),
        "1000-usdc") != digest


def test_http_discovery_quotes_fixed_fee_and_unsigned_retry_never_settles(
        monkeypatch):
    from app import main

    local, _agent, _wallet = _store(monkeypatch)
    monkeypatch.setattr(main, "store", local)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_NETWORK", NETWORK)
    monkeypatch.setenv("GUILD_X402_ASSET", ASSET)
    monkeypatch.setenv("GUILD_X402_PAY_TO", x402.MAINNET_TREASURY)
    monkeypatch.setattr(x402, "config_errors", lambda: [])
    endpoint = protectedmarket.tier_path("10000-usdc")
    with TestClient(main.app) as client:
        catalog = client.get("/wallet-binding/protected-decision/tiers")
        assert catalog.status_code == 200
        assert len(catalog.json()["tiers"]) == 5
        assert catalog.json()["tiers"][1]["marketplace"]["buy_url"] == (
            "https://payanagent.com/x402/"
            + protectedmarket.PAYAN_TIER_OFFERS["10000-usdc"])
        quote = client.post(endpoint, json={})
        assert quote.status_code == 402, quote.text
        detail = quote.json()["detail"]
        assert detail["discovery_only"] is True
        assert detail["executable"] is False
        assert detail["accepts"][0]["amount"] == "25000000"
        assert endpoint in detail["resource"]["url"]
        anonymous = client.post(
            endpoint, json={}, headers={"PAYMENT-SIGNATURE": "not-a-payment"})
        assert anonymous.status_code == 401
        assert anonymous.json()["detail"]["billing"] == "NOT CHARGED"
    assert not [r for r in local.billing_log if r.get("type") == "x402_payment"]


def test_verified_body_returns_exact_payan_bound_quote(monkeypatch):
    from app import main

    local, _agent, wallet = _store(monkeypatch)
    monkeypatch.setattr(main, "store", local)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_NETWORK", NETWORK)
    monkeypatch.setenv("GUILD_X402_ASSET", ASSET)
    monkeypatch.setenv("GUILD_X402_PAY_TO", x402.MAINNET_TREASURY)
    monkeypatch.setattr(x402, "config_errors", lambda: [])
    tier_id = "1000-usdc"
    relay = "https://payanagent.com/x402/kh_protected_tier_1000"
    semantic = _request(wallet.address, tier_id, relay)
    wrapped, _caller = _wrapped(
        "0x" + "47" * 32, tier_id, semantic, "protected-tier-quote")
    with TestClient(main.app) as client:
        quote = client.post(protectedmarket.tier_path(tier_id), json=wrapped)
        assert quote.status_code == 402, quote.text
        detail = quote.json()["detail"]
        assert detail["accepts"][0]["amount"] == "2500000"
        assert detail["resource"]["url"] == relay


def test_signed_result_verifier_seals_tier_and_relay(monkeypatch):
    local, _agent, wallet = _store(monkeypatch)
    tier_id = "1000-usdc"
    relay = "https://payanagent.com/x402/kh_protected_tier_1000"
    semantic = _request(wallet.address, tier_id, relay)
    caller = callerproof.evm_did("0x" + "44" * 20)
    normalized = protectedmarket.normalise_request(semantic, tier_id)
    now = datetime(2026, 8, 12, 16, 0, tzinfo=timezone.utc)
    decision = protectedmarket.issue(
        local, semantic, tier_id, caller_did=caller, now=now)
    assert decision["credentialSubject"]["policy"]["requested"] == {
        "max_risk": 30.0, "min_confidence": 0.7}
    verified = protectedmarket.verify(
        decision, expected_request=semantic, tier_id=tier_id, now=now)
    assert verified["valid"] is True
    changed = {**semantic, "x402_resource_url":
               "https://payanagent.com/x402/kh_protected_tier_other"}
    assert protectedmarket.verify(
        decision, expected_request=changed, tier_id=tier_id,
        now=now)["valid"] is False


def test_gateway_builder_uses_tier_path_or_exact_payan_alias():
    quote = protectedmarket.tier_quote("100000-usdc")
    direct = payments.protected_payment_tier_request(
        "100000-usdc", "ab" * 32, quote)
    assert direct.cost == 250000
    assert direct.path.endswith("/tiers/100000-usdc")
    assert direct.resource_url.startswith(
        x402.public_host() + "/wallet-binding/protected-decision/tiers/")
    relay = "https://payanagent.com/x402/kh_protected_tier_100k"
    proxied = payments.protected_payment_tier_request(
        "100000-usdc", "ab" * 32, quote, relay)
    assert proxied.resource_url == relay
    assert proxied.cost == direct.cost
