"""Truthful OpenAPI payment discovery for autonomous HTTP buyers.

``/.well-known/x402`` remains the canonical origin fan-out and every live 402
remains authoritative for settlement.  Some machine discovery clients,
however, intentionally discover paid operations from OpenAPI only.  This
module projects the prices enforced by :mod:`app.payments` into the structured
``x-payment-info`` profile those clients understand.

The projection is deliberately generated on every OpenAPI read.  Four prices
can move at runtime under the bounded experiment engine; caching their values
inside FastAPI's otherwise-cached schema would advertise a quote the gateway
may no longer honour.
"""
from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any, Callable

from fastapi import FastAPI

from . import billing, pricing, protecteddecision, protectedmarket, x402


_CREDIT_USD = Decimal(str(billing.CREDIT_USD))


def _usd(credits: int) -> str:
    """Exact decimal USD text for an integer number of Guild credits."""
    value = Decimal(int(credits)) * _CREDIT_USD
    text = format(value, "f").rstrip("0").rstrip(".")
    return text or "0"


def _fixed(credits: int) -> dict[str, Any]:
    return {
        "price": {
            "mode": "fixed", "currency": "USD", "amount": _usd(credits),
        },
        "protocols": [{"x402": {"version": 2}}],
    }


def _dynamic(min_credits: int, max_credits: int) -> dict[str, Any]:
    return {
        "price": {
            "mode": "dynamic", "currency": "USD",
            "min": _usd(min_credits), "max": _usd(max_credits),
        },
        "protocols": [{"x402": {"version": 2}}],
    }


def _self_or_third_party(operation: str) -> dict[str, Any]:
    """A subject read is free; the same read about another agent is paid."""
    return _dynamic(0, billing.PRICING[operation])


def _protected() -> dict[str, Any]:
    terms = protecteddecision.schedule()
    return _dynamic(
        int(terms["minimum_fee_credits"]),
        int(terms["maximum_fee_credits"]),
    )


def _protected_tiers() -> dict[str, Any]:
    fees = [int(row["service_quote"]["fee_credits"])
            for row in protectedmarket.catalog()]
    return _dynamic(min(fees), max(fees))


# Exact FastAPI path templates and methods.  Only operations that can really
# require payment are included.  Free verification/catalog routes and watch
# provisioning are intentionally absent.
_PROFILES: dict[tuple[str, str], Callable[[], dict[str, Any]]] = {
    ("/check", "get"): lambda: _dynamic(
        billing.PRICING["best_agent"], billing.PRICING["signed_decision"]),
    ("/check/decision", "post"): lambda: _fixed(
        billing.PRICING["signed_decision"]),
    ("/search", "get"): lambda: _fixed(billing.PRICING["best_agent"]),
    ("/agents/{agent_id}/reputation", "get"): lambda: (
        _self_or_third_party("reputation")),
    ("/agents/{agent_id}/journey", "get"): lambda: (
        _self_or_third_party("reputation")),
    ("/agents/{agent_id}/evidence", "get"): lambda: (
        _self_or_third_party("evidence")),
    ("/agents/{agent_id}/flags", "get"): lambda: _fixed(
        billing.PRICING["fraud_check"]),
    ("/agents/{agent_id}/risk-score", "get"): lambda: _fixed(
        billing.PRICING["risk_score"]),
    ("/flags", "get"): lambda: _fixed(billing.PRICING["fraud_check"]),
    ("/preflight/deep", "get"): lambda: _fixed(
        pricing.price("deep_preflight")),
    ("/evidence/bundle", "post"): lambda: _fixed(
        pricing.price("evidence_bundle")),
    ("/envelopes/issue", "post"): lambda: _fixed(
        pricing.price("machine_envelope")),
    ("/wallet-binding/decision", "post"): lambda: _fixed(
        pricing.price("payment_decision")),
    ("/wallet-binding/protected-decision", "post"): _protected,
    ("/wallet-binding/protected-decision/tiers/{tier_id}", "post"):
        _protected_tiers,
}


def apply(schema: dict[str, Any]) -> dict[str, Any]:
    """Apply the live payment projection to a fresh OpenAPI schema copy."""
    host = x402.public_host()
    info = schema.setdefault("info", {})
    info["x-guidance"] = (
        "Payable machine utilities are ordered first in paths. Budget from "
        "x-payment-info, then treat the live HTTP 402 PAYMENT-REQUIRED header "
        "as authoritative. Verification and catalogs stay free. Routes that "
        "require caller proof may return a non-executable discovery quote to "
        "an empty probe; an unsigned paid retry is rejected before settlement."
    )
    schema["x-agentcash-provenance"] = {
        "ownershipProofs": [
            host + "/.well-known/agent-guild-did.json",
            host + "/release",
            "https://github.com/AgentTanuki/agent-guild",
        ],
    }
    schema["x-agentcash-guidance"] = {"llmsTxtUrl": host + "/llms.txt"}

    paths = schema.get("paths") or {}
    for (path, method), factory in _PROFILES.items():
        operation = (paths.get(path) or {}).get(method)
        if not isinstance(operation, dict):
            # Fail closed in tests via advertised_operations(); never invent a
            # dead OpenAPI operation if a route is renamed.
            continue
        operation["security"] = []
        operation["x-payment-info"] = factory()
        operation["x-agent-guild-payment"] = {
            "settlement": "USDC via x402 v2",
            "network": x402.network(),
            "asset": x402.asset(),
            "enabled": x402.enabled(),
            "live_quote_authoritative": True,
            "payment_required_header": "PAYMENT-REQUIRED",
            "payment_signature_header": "PAYMENT-SIGNATURE",
            "pricing_url": host + "/pricing",
        }
        responses = operation.setdefault("responses", {})
        responses.setdefault("402", {
            "description": (
                "Payment Required. Parse the x402 v2 PAYMENT-REQUIRED header; "
                "its exact resource, amount, asset, network and recipient are "
                "authoritative for this request."),
        })

    # Preserve the complete OpenAPI contract, but put the commercial machine
    # utilities before the large free/admin surface.  Discovery clients retain
    # every route while token-capped agents encounter the products first.
    paid_path_order = list(dict.fromkeys(path for path, _ in _PROFILES))
    schema["paths"] = {
        **{path: paths[path] for path in paid_path_order if path in paths},
        **{path: item for path, item in paths.items()
           if path not in paid_path_order},
    }
    return schema


def advertised_operations() -> set[tuple[str, str]]:
    """Stable test/contract view of the operations projected as payable."""
    return set(_PROFILES)


def install(app: FastAPI) -> None:
    """Wrap FastAPI's schema builder while retaining its route/schema cache."""
    if getattr(app.state, "openapi_payment_discovery_installed", False):
        return
    base_openapi = app.openapi

    def live_openapi() -> dict[str, Any]:
        # FastAPI caches the expensive route-to-schema conversion.  Deep-copy
        # that structural base, then read current prices into the copy.
        return apply(copy.deepcopy(base_openapi()))

    app.openapi = live_openapi  # type: ignore[method-assign]
    app.state.openapi_payment_discovery_installed = True
