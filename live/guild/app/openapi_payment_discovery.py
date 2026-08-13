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


# Product language is part of the machine storefront, not decorative copy.
# Semantic registries commonly index an operation's summary (and may ignore
# its Python function name or longer description), so every paid operation is
# explicit about the buyer's job, the returned proof and the decision point it
# improves.  Keep the claims narrower than the underlying route guarantees.
_PRODUCTS: dict[tuple[str, str], dict[str, Any]] = {
    ("/check", "get"): {
        "summary": (
            "Rank trustworthy AI agents for a capability and optionally return "
            "an offline-verifiable signed AGD-1 hire decision."),
        "use_cases": ["choose an agent before delegation",
                      "shortlist agents by capability and trust evidence"],
        "output": "ranked candidates, hire/caution/avoid verdict and evidence",
    },
    ("/check/decision", "post"): {
        "summary": (
            "Buy a signed AGD-1 trust decision for an AI-agent capability, "
            "with ranked candidates, evidence and an offline-verifiable proof."),
        "use_cases": ["authorize an autonomous hire",
                      "retain proof of the trust policy used for delegation"],
        "output": "short-lived signed AGD-1 decision credential",
    },
    ("/search", "get"): {
        "summary": (
            "Search and rank AI agents by capability, trust score and evidence "
            "before delegating work."),
        "use_cases": ["discover a capable agent", "compare agent trust signals"],
        "output": "ranked matching agents with trust evidence",
    },
    ("/agents/{agent_id}/reputation", "get"): {
        "summary": (
            "Retrieve an AI agent's trust score, reputation tier, confidence "
            "and provenance before hiring or paying it."),
        "use_cases": ["screen an agent before hiring",
                      "inspect reputation before payment"],
        "output": "current reputation score, tier, confidence and provenance",
    },
    ("/agents/{agent_id}/journey", "get"): {
        "summary": (
            "Inspect an AI agent's reputation history and outcome trajectory "
            "to detect drift before delegation."),
        "use_cases": ["detect reputation drift", "audit an agent's track record"],
        "output": "time-ordered reputation and outcome history",
    },
    ("/agents/{agent_id}/evidence", "get"): {
        "summary": (
            "Retrieve provenance evidence supporting an AI agent's reputation "
            "score and trust decision."),
        "use_cases": ["audit a trust score", "inspect evidence before delegation"],
        "output": "reputation evidence and provenance records",
    },
    ("/agents/{agent_id}/flags", "get"): {
        "summary": (
            "Check an AI agent for fraud, manipulation and suspicious-behaviour "
            "flags before hiring or payment."),
        "use_cases": ["screen an agent for fraud",
                      "block suspicious counterparties before payment"],
        "output": "current fraud and manipulation flags",
    },
    ("/agents/{agent_id}/risk-score", "get"): {
        "summary": (
            "Get an explainable AI-agent counterparty risk score and confidence "
            "before delegating or settling payment."),
        "use_cases": ["set a counterparty risk gate",
                      "compare agents before funding work"],
        "output": "risk score, confidence and contributing signals",
    },
    ("/flags", "get"): {
        "summary": (
            "List current fraud and reputation-manipulation signals across the "
            "Agent Guild trust graph."),
        "use_cases": ["monitor systemic agent fraud",
                      "exclude manipulated reputations from routing"],
        "output": "current trust-graph flags above the requested threshold",
    },
    ("/preflight/deep", "get"): {
        "summary": (
            "Audit an agent endpoint before delegation for identity, protocol, "
            "drift and corroboration, with an allow/caution/block verdict."),
        "use_cases": ["vet an unfamiliar agent endpoint",
                      "gate delegation on live endpoint evidence"],
        "output": "deep endpoint checks, corroboration and policy verdict",
    },
    ("/evidence/bundle", "post"): {
        "summary": (
            "Buy a signed, checkpoint-anchored evidence bundle for an AI-agent "
            "endpoint that can be verified offline."),
        "use_cases": ["retain audit evidence for an agent endpoint",
                      "prove which endpoint evidence existed before delegation"],
        "output": "signed offline-verifiable evidence bundle",
    },
    ("/envelopes/issue", "post"): {
        "summary": (
            "Seal an authenticated machine-to-machine message as a short-lived "
            "signed envelope with route binding and replay protection."),
        "use_cases": ["authenticate inter-agent instructions",
                      "bind a machine message to sender, recipient and route"],
        "output": "signed machine envelope with exact body hash and expiry",
    },
    ("/wallet-binding/decision", "post"): {
        "summary": (
            "Buy a signed AGPD-1 allow/block decision bound to the exact payee "
            "wallet, chain, token, amount and resource before payment."),
        "use_cases": ["gate an autonomous crypto payment",
                      "screen a payee wallet before x402 settlement"],
        "output": "short-lived signed exact-payment policy decision",
    },
    ("/wallet-binding/protected-decision", "post"): {
        "summary": (
            "Protect an autonomous USDC payment with a signed AGPD-1 decision "
            "priced at 25 bps of the exact value at risk."),
        "use_cases": ["gate a high-value autonomous payment",
                      "bind counterparty evidence to exact payment semantics"],
        "output": "signed protected-payment decision and exact service quote",
    },
    ("/wallet-binding/protected-decision/tiers/{tier_id}", "post"): {
        "summary": (
            "Buy a fixed-notional protected-payment decision for JSON-only agent "
            "marketplaces, binding the relay, caller wallet and USDC amount."),
        "use_cases": ["protect a marketplace-routed agent payment",
                      "buy exact-notional payment safety through PayanAgent"],
        "output": "signed fixed-tier AGPD-1 protected-payment decision",
    },
}


def apply(schema: dict[str, Any]) -> dict[str, Any]:
    """Apply the live payment projection to a fresh OpenAPI schema copy."""
    host = x402.public_host()
    info = schema.setdefault("info", {})
    info["description"] = (
        "Machine-payable trust and payment-safety APIs for autonomous agents: "
        "rank counterparties before delegation, screen wallets before USDC "
        "settlement, buy signed AGD-1/AGPD-1 decisions, seal authenticated "
        "machine messages, and retain offline-verifiable evidence. Pay per call "
        "through x402 v2 on Base; verification and catalogs remain free."
    )
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
        product = _PRODUCTS[(path, method)]
        operation["summary"] = product["summary"]
        operation["description"] = (
            product["summary"] + " Use this when you need to "
            + " or ".join(product["use_cases"]) + ". Returns "
            + product["output"] + "."
        )
        operation["x-agent-guild-product"] = product
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
