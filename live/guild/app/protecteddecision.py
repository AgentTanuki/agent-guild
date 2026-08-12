"""Value-based protection for exact autonomous USDC payments.

The ordinary AGPD-1 route is deliberately cheap: it is useful for routine
machine payments.  This module defines the higher-assurance product for value
that deserves evidence-depth and live-routing gates.  Its service fee is a
transparent fraction of the exact Base-mainnet USDC transfer being protected,
not a marketing tier selected by the caller.

The quote is deterministic, integer-only and signed into the resulting
credential.  The x402 settlement resource carries the quote version, basis
points and exact fee, while the opaque request digest binds the protected
payee, token, amount, resource and policy.
"""
from __future__ import annotations

import hashlib
from typing import Any

from . import paymentdecision, reachability, x402
from . import crypto

CONTRACT = "agent-guild/protected-value-policy/v1"
PRICING_VERSION = "agent-guild/protected-value-pricing/v1"
DEFAULT_FEE_BPS = 25                 # 0.25% of protected value
DEFAULT_MIN_FEE_CREDITS = 10         # $0.01
DEFAULT_MAX_FEE_CREDITS = 10_000_000 # $10,000 hard safety ceiling
USDC_ATOMIC_PER_CREDIT = 1_000       # one credit is $0.001
BASE_MAINNET = "eip155:8453"
BASE_USDC = x402.USDC_BY_NETWORK[BASE_MAINNET].lower()


class ProtectedDecisionRefused(paymentdecision.PaymentDecisionRefused):
    """The request cannot receive the protected-value product. Never bill."""

    code = "protected_payment_decision_refused"


def schedule() -> dict[str, Any]:
    """Return the immutable fee schedule identified by ``PRICING_VERSION``.

    This is intentionally not environment-overridable: buyers precompute the
    exact quote locally, and the pricing contract is signed into every result.
    Any economic change therefore requires a new public pricing version rather
    than silently changing what an existing version means.
    """
    return {
        "contract": PRICING_VERSION,
        "basis_points": DEFAULT_FEE_BPS,
        "minimum_fee_credits": DEFAULT_MIN_FEE_CREDITS,
        "maximum_fee_credits": DEFAULT_MAX_FEE_CREDITS,
        "credit_usd": 0.001,
        "rounding": "ceil to the next 0.001 USDC credit, then clamp",
        "network": BASE_MAINNET,
        "asset": BASE_USDC,
        "immutable": True,
    }


def normalise_request(request: Any) -> dict[str, Any]:
    """Normalize AGPD-1 semantics and restrict protection to real Base USDC."""
    if not isinstance(request, dict):
        raise ProtectedDecisionRefused("request must be an object")
    allowed = {"payment", "capability", "policy", "ttl_seconds"}
    unknown = sorted(set(request) - allowed)
    if unknown:
        raise ProtectedDecisionRefused(
            "unsupported fields: " + ", ".join(unknown))
    raw_payment = request.get("payment")
    if not isinstance(raw_payment, dict):
        raise ProtectedDecisionRefused("payment object is required")
    payment_fields = {
        "scheme", "network", "asset", "amount", "pay_to", "resource"}
    payment_unknown = sorted(set(raw_payment) - payment_fields)
    payment_missing = sorted(payment_fields - set(raw_payment))
    if payment_unknown:
        raise ProtectedDecisionRefused(
            "unsupported payment fields: " + ", ".join(payment_unknown))
    if payment_missing:
        raise ProtectedDecisionRefused(
            "missing payment fields: " + ", ".join(payment_missing))
    raw_policy = request.get("policy")
    if raw_policy is not None:
        if not isinstance(raw_policy, dict):
            raise ProtectedDecisionRefused("policy must be an object")
        policy_unknown = sorted(
            set(raw_policy) - {"max_risk", "min_confidence"})
        if policy_unknown:
            raise ProtectedDecisionRefused(
                "unsupported policy fields: " + ", ".join(policy_unknown))
    try:
        normalized = paymentdecision.normalise_request(request)
    except paymentdecision.PaymentDecisionRefused as exc:
        raise ProtectedDecisionRefused(str(exc)) from exc
    payment = normalized["payment"]
    if payment["scheme"] != "exact":
        raise ProtectedDecisionRefused(
            "protected decisions require the exact payment scheme")
    if payment["network"] != BASE_MAINNET:
        raise ProtectedDecisionRefused(
            "protected decisions require Base mainnet (eip155:8453)")
    if payment["asset"] != BASE_USDC:
        raise ProtectedDecisionRefused(
            "protected decisions require canonical Base mainnet USDC")
    return normalized


def _value_tier(amount_atomic: int) -> str:
    # Same dollar bands as the Guild's market value-at-risk contract, expressed
    # in six-decimal USDC atomic units rather than sandbox credits.
    if amount_atomic < 10_000_000:
        return "micro"
    if amount_atomic < 100_000_000:
        return "low"
    if amount_atomic < 1_000_000_000:
        return "medium"
    return "high"


def _quote_normalized(normalized: dict[str, Any]) -> dict[str, Any]:
    amount = int(normalized["payment"]["amount"])
    terms = schedule()
    # ceil(amount * bps / 10_000) produces USDC atomic units; a second ceil
    # maps that to the gateway's $0.001 credit granularity.
    raw_fee_atomic = (amount * terms["basis_points"] + 9_999) // 10_000
    raw_fee_credits = (
        raw_fee_atomic + USDC_ATOMIC_PER_CREDIT - 1
    ) // USDC_ATOMIC_PER_CREDIT
    fee_credits = max(
        terms["minimum_fee_credits"],
        min(raw_fee_credits, terms["maximum_fee_credits"]),
    )
    return {
        **terms,
        "protected_amount_atomic": str(amount),
        "protected_value_tier": _value_tier(amount),
        "fee_credits": fee_credits,
        "fee_atomic_usdc": str(fee_credits * USDC_ATOMIC_PER_CREDIT),
        "fee_usd": fee_credits / 1_000,
        "clamped_to_minimum": raw_fee_credits < terms["minimum_fee_credits"],
        "clamped_to_maximum": raw_fee_credits > terms["maximum_fee_credits"],
    }


def quote(request: Any) -> dict[str, Any]:
    """Quote the exact service fee for the exact protected payment."""
    return _quote_normalized(normalise_request(request))


def request_sha256(request: Any, caller_did: str) -> str:
    """Bind normalized semantics to the machine EOA buying the decision."""
    normalized = normalise_request(request)
    if not isinstance(caller_did, str) or not caller_did:
        raise ProtectedDecisionRefused("a verified caller DID is required")
    return hashlib.sha256(crypto.canonicalize_jcs({
        "request": normalized,
        "caller_did": caller_did,
    }).encode("utf-8")).hexdigest()


def discovery_quote() -> dict[str, Any]:
    """Non-executable registry quote; real pricing requires a signed body."""
    terms = schedule()
    credits = terms["minimum_fee_credits"]
    return {
        **terms,
        "protected_amount_atomic": None,
        "protected_value_tier": None,
        "fee_credits": credits,
        "fee_atomic_usdc": str(credits * USDC_ATOMIC_PER_CREDIT),
        "fee_usd": credits / 1_000,
        "discovery_only": True,
    }


def evaluate(resolution: dict[str, Any], risk: dict[str, Any] | None,
             provenance: dict[str, Any] | None,
             service_quote: dict[str, Any], store: Any,
             caller_did: str) -> dict[str, Any]:
    """Apply value-tier and live-routing gates beyond ordinary AGPD-1."""
    failures: list[str] = []
    agent = resolution.get("agent") or {}
    endpoint = agent.get("endpoint")
    routing = reachability.reachability_fields(
        endpoint, agent.get("reachability"))
    if not routing.get("recommended_for_routing"):
        failures.append("counterparty_endpoint_not_verified_reachable")

    try:
        value_support = store._value_at_risk_support(
            provenance or {}, float((risk or {}).get("confidence", 0)),
            (risk or {}).get("staleness"))
    except Exception:
        value_support = {
            "tiers": {"micro": False, "low": False,
                      "medium": False, "high": False},
            "max_supported_tier": None,
            "basis": "value-at-risk evidence evaluation unavailable",
        }
    required_tier = service_quote["protected_value_tier"]
    evidence_supported = bool(
        (value_support.get("tiers") or {}).get(required_tier, False))
    if not evidence_supported:
        failures.append("evidence_depth_below_protected_value_tier")
    actively_bound = bool(
        resolution.get("status") == "bound_registered" and agent)
    risk_available = risk is not None
    return {
        "contract": CONTRACT,
        "pricing": service_quote,
        "required_value_tier": required_tier,
        "value_at_risk": value_support,
        "reachability": routing,
        "caller_payer_binding_required": True,
        "service_client": {
            "caller_did": caller_did,
            "payer_eoa": caller_did.rsplit(":", 1)[-1].lower(),
            "rule": (
                "the EIP-191 caller-proof EOA must exactly equal the EIP-3009 "
                "payer of this decision's x402 fee; enforced before settlement"),
        },
        "gates": {
            "exact_base_mainnet_usdc": True,
            "active_wallet_binding": actively_bound,
            "current_risk_evidence_available": risk_available,
            "verified_reachable_endpoint": bool(
                routing.get("recommended_for_routing")),
            "evidence_supports_value_tier": evidence_supported,
        },
        "failures": failures,
        "limits": (
            "This decision reduces counterparty identity, evidence-depth and "
            "routing risk at issuance time. It is not insurance, escrow, a "
            "guarantee of delivery, or a guarantee the recipient will refund."
        ),
    }


def verify(decision: Any, *, expected_request: Any,
           now=None) -> dict[str, Any]:
    """Verify signature, freshness, exact request and deterministic quote."""
    base = paymentdecision.verify(
        decision, expected_request=expected_request, now=now)
    try:
        expected_quote = quote(expected_request)
    except ProtectedDecisionRefused:
        expected_quote = None
    subject = decision.get("credentialSubject") if isinstance(decision, dict) else {}
    protection = (subject or {}).get("protection") or {}
    quote_exact = bool(expected_quote is not None
                       and protection.get("pricing") == expected_quote)
    contract_valid = protection.get("contract") == CONTRACT
    return {
        **base,
        "valid": bool(base.get("valid") and quote_exact and contract_valid),
        "protected_value_contract_valid": contract_valid,
        "service_quote_exact": quote_exact,
    }


def issue_extension(store: Any, normalized: dict[str, Any],
                    resolution: dict[str, Any], risk: dict[str, Any] | None,
                    provenance: dict[str, Any] | None, *, caller_did: str
                    ) -> dict[str, Any]:
    """Build the signed subject extension inside the one AGPD-1 issue path."""
    return evaluate(
        resolution, risk, provenance, _quote_normalized(normalized), store,
        caller_did)
