"""Fixed-notional marketplace transport for protected payment decisions.

JSON-only marketplaces can relay one fixed x402 price per listing but cannot
truthfully advertise a body-derived percentage fee.  These tiers expose three
exact USDC notionals.  They do not discount or approximate the protected-value
schedule: each listing's price is the same immutable 25 bps quote the dynamic
route would compute for that exact amount.
"""
from __future__ import annotations

from typing import Any

from . import crypto, marketpaymentdecision, paymentdecision, protecteddecision
from .x402_artifacts import sha256_hex

CONTRACT = "agent-guild/protected-marketplace-tier/v1"

# PayanAgent currently accepts a maximum offer price of $100,000.  These
# protected notionals keep the service fee well inside that limit while making
# valuable autonomous payments directly discoverable in its fixed-price
# catalog.  Amounts are canonical Base USDC atomic integers (six decimals).
TIERS: dict[str, str] = {
    "1000-usdc": "1000000000",
    "10000-usdc": "10000000000",
    "100000-usdc": "100000000000",
    "1000000-usdc": "1000000000000",
    "4000000-usdc": "4000000000000",
}

# Canonical PayanAgent relay listings owned by Agent Guild's treasury seller.
# A marketplace buyer must sign the exact buy URL into caller_proof, so omitting
# these identifiers from the free catalog made an otherwise-live product hard
# to purchase programmatically.  Keep the mapping explicit and release-bound:
# silently discovering a replacement at request time would let a third party
# change the URL that buyers are asked to sign.
PAYAN_SELLER_ID = "j5745s1y3cy11gbz8592yagyn18c1b12"
PAYAN_TIER_OFFERS: dict[str, str] = {
    "1000-usdc": "kh73ayftag0772zh0rx5f0rrp58cbkcc",
    "10000-usdc": "kh7cn16zdkhdk56rn51sbmv5yx8cavrk",
    "100000-usdc": "kh71s9j5932pebjq596egk93558cbxjk",
    "1000000-usdc": "kh782cngmpkmx1jxnwf7v5hdyx8cbrzr",
    "4000000-usdc": "kh743b8n09qnxq2tqnwyb4bc6d8camnh",
}
PAYAN_ORIGIN = "https://payanagent.com"


class ProtectedMarketplaceRefused(ValueError):
    code = "protected_marketplace_tier_refused"


def tier_path(tier_id: str) -> str:
    if tier_id not in TIERS:
        raise ProtectedMarketplaceRefused("unknown protected-value tier")
    return f"/wallet-binding/protected-decision/tiers/{tier_id}"


def tier_quote(tier_id: str) -> dict[str, Any]:
    """Return the exact ordinary protected-value quote for this notional."""
    amount = TIERS.get(tier_id)
    if amount is None:
        raise ProtectedMarketplaceRefused("unknown protected-value tier")
    # The payee and resource do not affect the deterministic service fee.
    return protecteddecision.quote({
        "payment": {
            "scheme": "exact",
            "network": protecteddecision.BASE_MAINNET,
            "asset": protecteddecision.BASE_USDC,
            "amount": amount,
            "pay_to": "0x0000000000000000000000000000000000000001",
            "resource": "https://example.invalid/protected-payment",
        },
    })


def catalog() -> list[dict[str, Any]]:
    rows = []
    for tier_id, amount in TIERS.items():
        offer_id = PAYAN_TIER_OFFERS[tier_id]
        buy_url = f"{PAYAN_ORIGIN}/x402/{offer_id}"
        rows.append({
            "tier_id": tier_id,
            "path": tier_path(tier_id),
            "protected_amount_atomic": amount,
            "protected_amount_usdc": int(amount) // 1_000_000,
            "service_quote": tier_quote(tier_id),
            "marketplace_transport": "strict JSON {request, caller_proof}",
            "marketplace": {
                "provider": "PayanAgent",
                "seller_id": PAYAN_SELLER_ID,
                "offer_id": offer_id,
                "offer_url": f"{PAYAN_ORIGIN}/marketplace/offers/{offer_id}",
                "buy_url": buy_url,
                "request_binding": {"x402_resource_url": buy_url},
            },
        })
    return rows


def normalise_request(body: Any, tier_id: str) -> dict[str, Any]:
    """Normalize exact AGPD-1 semantics plus the signed Payan buy URL."""
    try:
        normalized = marketpaymentdecision.normalise_request(body)
        # marketpaymentdecision already applies the complete AGPD-1
        # normalizer. Project its canonical core directly so the caller's
        # stricter policy is preserved exactly rather than normalized twice.
        protected = _normalized_core(normalized)
    except (marketpaymentdecision.MarketplacePaymentDecisionRefused,
            protecteddecision.ProtectedDecisionRefused) as exc:
        raise ProtectedMarketplaceRefused(str(exc)) from exc
    payment = protected["payment"]
    if payment["scheme"] != "exact":
        raise ProtectedMarketplaceRefused(
            "protected decisions require the exact payment scheme")
    if payment["network"] != protecteddecision.BASE_MAINNET:
        raise ProtectedMarketplaceRefused(
            "protected decisions require Base mainnet (eip155:8453)")
    if payment["asset"] != protecteddecision.BASE_USDC:
        raise ProtectedMarketplaceRefused(
            "protected decisions require canonical Base mainnet USDC")
    expected = TIERS.get(tier_id)
    if expected is None:
        raise ProtectedMarketplaceRefused("unknown protected-value tier")
    if protected["payment"]["amount"] != expected:
        raise ProtectedMarketplaceRefused(
            f"payment.amount must equal the {tier_id} tier's exact atomic "
            f"USDC amount ({expected})")
    return {**protected, "x402_resource_url": normalized["x402_resource_url"]}


def agpd_request(body: Any, tier_id: str) -> dict[str, Any]:
    """Return only fields sealed by the base AGPD-1 credential contract."""
    normalized = normalise_request(body, tier_id)
    return {
        "payment": normalized["payment"],
        "capability": normalized["capability"],
        "policy": normalized["policy"]["requested"],
        "ttl_seconds": normalized["ttl_seconds"],
    }


def _normalized_core(normalized: dict[str, Any]) -> dict[str, Any]:
    """Project a marketplace normalization into the AGPD normalized shape."""
    return {
        "payment": normalized["payment"],
        "capability": normalized["capability"],
        "policy": normalized["policy"],
        "ttl_seconds": normalized["ttl_seconds"],
    }


def request_sha256(body: Any, caller_did: str, tier_id: str) -> str:
    """Bind exact protected semantics, tier, relay URL and buyer EOA."""
    normalized = normalise_request(body, tier_id)
    if not isinstance(caller_did, str) or not caller_did:
        raise ProtectedMarketplaceRefused("a verified caller DID is required")
    return sha256_hex(crypto.canonicalize_jcs({
        "contract": CONTRACT,
        "tier_id": tier_id,
        "request": normalized,
        "caller_did": caller_did,
    }).encode("utf-8"))


def signed_extension(tier_id: str, normalized: dict[str, Any]) -> dict[str, Any]:
    """Facts added to the signed protection block for offline verification."""
    return {
        "contract": CONTRACT,
        "tier_id": tier_id,
        "endpoint": tier_path(tier_id),
        "x402_resource_url": normalized["x402_resource_url"],
        "protected_amount_atomic": TIERS[tier_id],
        "fixed_notional": True,
        "pricing_rule": (
            "the listing price is the ordinary protected-value schedule's "
            "exact 25 bps fee for this exact notional; no discount or markup"),
    }


def issue(store: Any, body: Any, tier_id: str, *, caller_did: str,
          now=None) -> dict[str, Any]:
    """Issue AGPD-1 while signing the exact marketplace tier and relay URL."""
    normalized = normalise_request(body, tier_id)
    core = _normalized_core(normalized)
    market_extension = signed_extension(tier_id, normalized)
    return paymentdecision.issue_normalized(
        store, core, now=now,
        policy_extension=lambda s, n, r, risk, prov: {
            **protecteddecision.issue_extension(
                s, n, r, risk, prov, caller_did=caller_did),
            "marketplace": market_extension,
        })


def verify(decision: Any, *, expected_request: Any,
           tier_id: str, now=None) -> dict[str, Any]:
    """Verify signature, exact AGPD request, tier and signed relay binding."""
    try:
        normalized = normalise_request(expected_request, tier_id)
        core = _normalized_core(normalized)
        expected_quote = tier_quote(tier_id)
        expected_marketplace = signed_extension(tier_id, normalized)
    except ProtectedMarketplaceRefused:
        return {
            "valid": False, "signature_valid": False,
            "exact_request": False, "protected_marketplace_tier_valid": False,
        }
    base = paymentdecision.verify_normalized(
        decision, expected_normalized=core, now=now)
    subject = (decision.get("credentialSubject")
               if isinstance(decision, dict) else {}) or {}
    protection = subject.get("protection") or {}
    exact_marketplace = protection.get("marketplace") == expected_marketplace
    exact_quote = protection.get("pricing") == expected_quote
    return {
        **base,
        "valid": bool(base.get("valid") and exact_marketplace and exact_quote),
        "protected_marketplace_tier_valid": exact_marketplace,
        "service_quote_exact": exact_quote,
        "tier_id": tier_id,
    }
