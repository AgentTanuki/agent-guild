"""Prices as CONFIGURATION, not doctrine.

Every price here is a reversible guess the experiment engine is allowed to
move. Hard-coding a number in a dict and treating it as settled is how a
business ends up defending a price it never measured.

Two rules this module exists to enforce:

1. **Every price is env-overridable** (``GUILD_PRICE_<OPERATION>``), so a price
   change is a config change and a rollback — not a deploy and a revert.
2. **Every price carries its rationale and its bounds.** A price with no stated
   basis cannot be argued with later, and a price with no ceiling can be moved
   by a buggy experiment to a number no machine will ever pay.

MEASURED BASIS (2026-07-31 — and the reason these numbers are low)
  Total x402 settled volume across all networks in July 2026 was $232,329,
  down 98.9% from the November 2025 peak on flat transaction count (~$0.04
  per transaction). The median x402 Bazaar listing took 2 calls and 1 unique
  payer in 30 days; the median earning agent made $1.65.

  A price set for an enterprise buyer is unpayable by that population. A price
  set at $0.001 cannot cover an outbound probe. So the opening prices sit just
  above marginal cost and far below the cost of the mistake they prevent — an
  irreversible x402 transfer to an endpoint that does not work. They are
  expected to be wrong, and expected to move.
"""
from __future__ import annotations

import os
from typing import Any

#: Opening prices in CREDITS (1 credit = $0.001). Basis, not doctrine.
DEFAULTS: dict[str, int] = {
    # High-volume machine communication primitive: one cent to bind an exact
    # payload digest to an authenticated sender, recipient, nonce and expiry,
    # then seal it with the Guild's did:key. Verification is always free.
    "machine_envelope": 10,
    # Exact pre-signature wallet policy: one cent to turn free identity
    # resolution + current risk into a short-lived, portable signed decision
    # bound to the actual x402 payment fields.
    "payment_decision": 10,
    # Several bounded outbound probes plus a policy verdict. $0.02 — half a
    # median x402 transaction, a fiftieth of the smallest transfer it guards.
    "deep_preflight": 20,
    # A signed, offline-verifiable artefact the caller keeps and can re-verify
    # without us. Below the signed-decision family: it attests to an
    # observation, not a recommendation.
    "evidence_bundle": 100,
    # Charged per recheck ACTUALLY performed.
    "watch_cycle": 5,
    # Provisioning is free — see RATIONALE.
    "watch_provision": 0,
    # The trust read itself (GET /check, GET /search, guild_check /
    # guild_search / guild_best_agent, the A2A capability ask). Priced at one
    # cent in billing.PRICING since the rail went live; listed here so the
    # catalogue, the OpenAPI storefront and the gateway all read ONE source
    # (pricing.price returned 0 for it before, so a catalogue entry would
    # have advertised $0 for a request the gateway bills).
    "best_agent": 10,
    # Guild-SIGNED offline-verifiable AGD-1 decision (GET /check?signed=true,
    # POST /check/decision). Same reasoning.
    "signed_decision": 1000,
}

#: Hard ceiling per operation. An experiment may move a price WITHIN these
#: bounds and nowhere else, so a runaway loop can neither price us out of the
#: market nor give the product away.
CEILINGS: dict[str, int] = {
    "machine_envelope": 1000,  # $1.00
    "payment_decision": 1000,  # $1.00
    "deep_preflight": 500,      # $0.50
    "evidence_bundle": 2000,    # $2.00
    "watch_cycle": 100,         # $0.10
    "watch_provision": 100,
    "best_agent": 1000,         # $1.00
    "signed_decision": 5000,    # $5.00
}

RATIONALE: dict[str, str] = {
    "machine_envelope": (
        "a high-volume, privacy-preserving non-repudiation primitive for "
        "machine messages and economic intents; opening at one cent keeps it "
        "below the value of the coordination error it prevents, while free "
        "verification makes every issued artefact more useful"),
    "payment_decision": (
        "a high-volume wallet safety primitive evaluated at the last reversible "
        "moment before signing; one cent is below the median x402 transfer and "
        "buys a portable signed record that binds the exact payee, chain, "
        "asset, amount and resource to current identity/risk evidence"),
    "deep_preflight": (
        "just above the marginal cost of several bounded outbound probes, and "
        "far below the cost of the mistake it prevents: an irreversible x402 "
        "transfer to an endpoint that does not work"),
    "evidence_bundle": (
        "a durable, offline-verifiable artefact the caller keeps and can "
        "re-verify without us; priced below the signed-decision family because "
        "it attests to an observation, not a recommendation"),
    "watch_cycle": (
        "charged per recheck ACTUALLY performed, so a quiet endpoint costs the "
        "customer nothing and we never bill for work we did not do"),
    "watch_provision": (
        "free — taking money before any observation exists would be charging "
        "for a promise"),
    "best_agent": (
        "the trust read every other product hangs off: one cent answers WHICH "
        "agent to delegate to, over the whole graph, before the caller risks "
        "the task or the payment; the unchanged price the gateway has charged "
        "since the rail went live, now readable from one source"),
    "signed_decision": (
        "a Guild-signed, offline-verifiable AGD-1 decision a THIRD party can "
        "re-verify without calling us; priced two orders above the unsigned "
        "read because the artefact is portable and the liability is ours"),
}


def _env_key(operation: str) -> str:
    return "GUILD_PRICE_" + operation.upper()


#: RUNTIME overrides applied by the autonomous experiment engine, mirrored here
#: from durable storage at boot. Held at module level (not passed through every
#: call site) because the payment gateway prices operations in code paths that
#: have no Store handle — and a price the engine cannot actually reach is a
#: suggestion, not a mechanism.
_RUNTIME: dict[str, int] = {}


def load_runtime(overrides: Optional[dict] = None) -> dict[str, int]:
    """Install persisted overrides (called by Store at boot and on change).

    Values are re-clamped on load, so a row written by an older build, or hand
    edited, can never exceed today's published ceiling."""
    _RUNTIME.clear()
    for op, value in (overrides or {}).items():
        if op not in DEFAULTS:
            continue                      # never invent an operation
        try:
            _RUNTIME[op] = max(0, min(int(value), CEILINGS[op]))
        except (TypeError, ValueError):
            continue
    return dict(_RUNTIME)


def runtime_overrides() -> dict[str, int]:
    return dict(_RUNTIME)


def price(operation: str) -> int:
    """The live price for `operation`, in credits.

    Precedence — ENVIRONMENT wins, deliberately:

        env override  →  runtime (experiment) override  →  default

    The human-set environment variable outranks the autonomous engine, so an
    operator can always pin a price and know the loop cannot move it back.
    That is the whole reason the engine writes to a separate layer instead of
    mutating the defaults.

    Everything is clamped to [0, CEILINGS[operation]]; a malformed value
    degrades to the next layer down rather than taking the endpoint offline or
    making it free by accident."""
    default = DEFAULTS.get(operation, 0)
    raw = os.environ.get(_env_key(operation))
    if raw is not None:
        try:
            return max(0, min(int(str(raw).strip()),
                              CEILINGS.get(operation, default)))
        except (TypeError, ValueError):
            pass                          # fall through to the runtime layer
    if operation in _RUNTIME:
        return _RUNTIME[operation]
    return default


def table() -> dict[str, Any]:
    """The public, machine-readable price list — with the basis attached.

    A price published without its rationale cannot be argued with, which is
    exactly the property a price should not have."""
    return {
        "unit": "credits",
        "credit_usd": 0.001,
        "prices": {
            op: {
                "credits": price(op),
                "usd": round(price(op) * 0.001, 4),
                "default_credits": DEFAULTS[op],
                "ceiling_credits": CEILINGS[op],
                "env_override": _env_key(op),
                "basis": RATIONALE[op],
                "overridden_by_env": os.environ.get(_env_key(op)) is not None,
                "runtime_override_credits": _RUNTIME.get(op),
                "precedence": "env > autonomous experiment override > default",
            }
            for op in DEFAULTS
        },
        "policy": (
            "Prices are configuration, not doctrine: each is an "
            "env-overridable, reversible guess with a stated basis and a hard "
            "ceiling. The autonomous experiment engine may move a price within "
            "its ceiling; it may not invent an operation, and it may not treat "
            "a price it has never measured as settled."),
        "measured_basis_2026_07": (
            "Total x402 settled volume across all networks in July 2026: "
            "$232,329, down 98.9% from the November 2025 peak on flat "
            "transaction count (~$0.04/tx). Median Bazaar listing: 2 calls, 1 "
            "unique payer per 30 days. Median earning agent: $1.65 per 30 "
            "days. These prices are set for THAT population."),
    }
