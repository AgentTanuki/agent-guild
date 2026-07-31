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
}

#: Hard ceiling per operation. An experiment may move a price WITHIN these
#: bounds and nowhere else, so a runaway loop can neither price us out of the
#: market nor give the product away.
CEILINGS: dict[str, int] = {
    "deep_preflight": 500,      # $0.50
    "evidence_bundle": 2000,    # $2.00
    "watch_cycle": 100,         # $0.10
    "watch_provision": 100,
}

RATIONALE: dict[str, str] = {
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
}


def _env_key(operation: str) -> str:
    return "GUILD_PRICE_" + operation.upper()


def price(operation: str) -> int:
    """The live price for `operation`, in credits.

    Resolution: environment override → default, clamped to
    [0, CEILINGS[operation]]. A malformed override degrades to the default
    rather than taking the endpoint offline or making it free by accident."""
    default = DEFAULTS.get(operation, 0)
    raw = os.environ.get(_env_key(operation))
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(0, min(value, CEILINGS.get(operation, default)))


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
                "overridden": os.environ.get(_env_key(op)) is not None,
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
