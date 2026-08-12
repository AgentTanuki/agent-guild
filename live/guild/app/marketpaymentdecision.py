"""Strict marketplace input for caller-bound AGPD-1 payment decisions."""
from __future__ import annotations

import re
from typing import Any

from . import paymentdecision
from .crypto import canonicalize_jcs
from .x402_artifacts import sha256_hex

_PAYAN_URL = re.compile(
    r"^https://payanagent\.com/x402/[A-Za-z0-9_-]{8,128}$")


class MarketplacePaymentDecisionRefused(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


def normalise_request(body: Any) -> dict[str, Any]:
    """Return one closed, fully normalized marketplace decision request.

    ``paymentdecision.normalise_request`` owns the AGPD-1 semantic rules. This
    wrapper adds the exact Payan relay URL and rejects unsigned/ignored sibling
    fields so a JSON marketplace cannot change the payment or settlement route.
    """
    if not isinstance(body, dict):
        raise MarketplacePaymentDecisionRefused(
            "invalid_request", "request must be an object")
    allowed = {
        "payment", "capability", "policy", "ttl_seconds",
        "x402_resource_url",
    }
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise MarketplacePaymentDecisionRefused(
            "unknown_fields", "unsupported fields: " + ", ".join(unknown))
    raw_payment = body.get("payment")
    if not isinstance(raw_payment, dict):
        raise MarketplacePaymentDecisionRefused(
            "invalid_payment", "payment must be an object")
    payment_fields = {
        "scheme", "network", "asset", "amount", "pay_to", "resource"}
    payment_unknown = sorted(set(raw_payment) - payment_fields)
    payment_missing = sorted(payment_fields - set(raw_payment))
    if payment_unknown:
        raise MarketplacePaymentDecisionRefused(
            "unknown_payment_fields",
            "unsupported payment fields: " + ", ".join(payment_unknown))
    if payment_missing:
        raise MarketplacePaymentDecisionRefused(
            "missing_payment_fields",
            "missing payment fields: " + ", ".join(payment_missing))
    capability = body.get("capability")
    if capability is not None and (
            not isinstance(capability, str) or len(capability.strip()) > 128):
        raise MarketplacePaymentDecisionRefused(
            "invalid_capability", "capability must be a string <= 128 chars")
    raw_policy = body.get("policy")
    if raw_policy is not None:
        if not isinstance(raw_policy, dict):
            raise MarketplacePaymentDecisionRefused(
                "invalid_policy", "policy must be an object")
        policy_unknown = sorted(
            set(raw_policy) - {"max_risk", "min_confidence"})
        if policy_unknown:
            raise MarketplacePaymentDecisionRefused(
                "unknown_policy_fields",
                "unsupported policy fields: " + ", ".join(policy_unknown))
    ttl = body.get("ttl_seconds", paymentdecision.DEFAULT_TTL_S)
    if (isinstance(ttl, bool) or not isinstance(ttl, int)
            or ttl < 60 or ttl > paymentdecision.MAX_TTL_S):
        raise MarketplacePaymentDecisionRefused(
            "invalid_ttl_seconds", "ttl_seconds must be an integer 60..3600")
    relay = body.get("x402_resource_url")
    if not isinstance(relay, str) or _PAYAN_URL.fullmatch(relay) is None:
        raise MarketplacePaymentDecisionRefused(
            "invalid_x402_resource_url",
            "x402_resource_url must be the exact canonical "
            "https://payanagent.com/x402/<offer-id> URL")
    try:
        normalized = paymentdecision.normalise_request(body)
    except paymentdecision.PaymentDecisionRefused as exc:
        raise MarketplacePaymentDecisionRefused(exc.code, str(exc)) from exc
    return {**normalized, "x402_resource_url": relay}


def agpd_request(body: Any) -> dict[str, Any]:
    """Return only the exact payment-policy semantics sealed into AGPD-1."""
    normalized = normalise_request(body)
    return {
        "payment": normalized["payment"],
        "capability": normalized["capability"],
        "policy": normalized["policy"]["requested"],
        "ttl_seconds": normalized["ttl_seconds"],
    }


def request_sha256(body: Any, caller_did: str) -> str:
    """Opaque settlement binding over exact semantics and authenticated caller."""
    normalized = normalise_request(body)
    if not isinstance(caller_did, str) or not caller_did:
        raise MarketplacePaymentDecisionRefused(
            "verified_caller_required", "an authenticated caller DID is required")
    return sha256_hex(canonicalize_jcs({
        "request": normalized,
        "caller_did": caller_did,
    }).encode("utf-8"))
