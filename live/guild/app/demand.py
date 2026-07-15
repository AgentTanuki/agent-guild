"""ONE shared, transport-neutral capability-demand recorder (B1, 2026-07-15).

The regression this closes: payment enforcement ran BEFORE `store.check()`
recorded demand, so an unpaid external actor asking for `korean-legal` got an
`x402_payment_required` challenge and the capability never appeared in
`/capabilities.unmet_demand`. A machine must never have to pay merely to tell
AG what capability it needs.

`record_demand()` is invoked by every transport BEFORE authorization /
challenge:

  * HTTP   — GET /check and GET /search, before `meter()`;
  * MCP    — guild_check/guild_search/guild_best_agent, before `authorize`;
  * A2A    — the `capability_ask` branch, before the x402 payment task.

Properties:
  * records genuine capability demand even when the caller does not pay;
  * deduplicates retries and payment resubmissions: one (actor, capability)
    ask per dedupe window (GUILD_DEMAND_DEDUPE_S, default 1h) counts once —
    via DURABLE keyed state (store.demand_dedupe), not an event-tail scan,
    so restarts and event floods cannot defeat it;
  * because the transports record BEFORE payment, the post-payment
    `store.check()` call passes `demand_recorded=True` and the same request
    is never counted again after payment succeeds;
  * preserves actor (namespaced/fingerprinted, never a raw IP), transport,
    timestamp and supplied/verified-reachable counts;
  * greetings and probes never reach this module (intent parsing happens
    first); registry crawlers and AG-owned traffic are recorded but flagged
    (ua/first-party) and excluded from genuine-external growth at read time
    (store.demand_feed_entries / attribution rules);
  * when exact usable supply is zero, `no_supply_block()` gives every
    transport the same FREE machine-readable next step: canonical
    capability, supplied + verified-reachable counts, a free demand-watch
    action, a supplier-registration action and a stable demand identifier —
    never the paid shortlist, trust scores or raw evidence.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime
from typing import Any, Optional

_CAP_ALLOWED = re.compile(r"[^a-z0-9_.\-]+")
_MAX_CAP_LEN = 64


def canonical_capability(capability: str) -> str:
    """Deterministic canonical form: lowercase, trimmed, spaces collapsed to
    hyphens, restricted charset, bounded length. Same normalisation on every
    transport so the same need aggregates to the same demand row."""
    cap = (capability or "").strip().lower()
    cap = re.sub(r"\s+", "-", cap)
    cap = _CAP_ALLOWED.sub("", cap)
    return cap[:_MAX_CAP_LEN]


def demand_id_for(capability: str) -> str:
    """Stable machine identifier for one canonical capability's demand."""
    canon = canonical_capability(capability)
    return "dem_" + hashlib.sha256(
        ("agent-guild/demand/" + canon).encode()).hexdigest()[:16]


def dedupe_window_s() -> float:
    try:
        return float(os.environ.get("GUILD_DEMAND_DEDUPE_S") or 3600.0)
    except ValueError:
        return 3600.0


def supply_counts(store: Any, canon: str) -> dict[str, int]:
    """Honest supply-side counts for one capability: registered suppliers,
    suppliers with a declared endpoint, and suppliers with a VERIFIED
    currently-reachable endpoint (the only ones /check would route to)."""
    entries = store.shortlist(canon, limit=10_000)
    return {
        "supplied": len(entries),
        "declared_endpoint": sum(1 for e in entries
                                 if e.get("has_declared_endpoint")),
        "verified_reachable": sum(1 for e in entries
                                  if e.get("recommended_for_routing")),
    }


def _parse_at(at: Any) -> float:
    try:
        return datetime.fromisoformat(
            str(at).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def record_demand(capability: str, *, transport: str, actor: str = "",
                  ua: str = "", first_party: bool = False,
                  ) -> Optional[dict[str, Any]]:
    """Record one capability ask BEFORE authorization. Returns the demand
    context (canonical capability, demand id, supply counts, whether this
    call was newly counted) for the transport to build its challenge /
    no_supply block from — or None for an empty/unusable capability."""
    from .state import store
    canon = canonical_capability(capability)
    if not canon:
        return None
    actor = actor or "anon"
    counts = supply_counts(store, canon)
    now = time.time()
    # DURABLE dedupe keyed (actor, capability, window): immune to event
    # floods, survives restarts (store.demand_dedupe_check_and_mark).
    counted = store.demand_dedupe_check_and_mark(
        actor, canon, now, dedupe_window_s())
    if counted:
        store.record_event(None, "capability_demand", ua=ua,
                           capability=canon,
                           explicit=True,
                           supplied=counts["supplied"] > 0,
                           reachable_supply=counts["declared_endpoint"] > 0,
                           supplied_count=counts["supplied"],
                           verified_reachable=counts["verified_reachable"],
                           transport=transport,
                           actor=actor,
                           demand_first_party=bool(first_party),
                           demand_id=demand_id_for(canon),
                           phase="pre_authorization")
    return {"capability": canon, "demand_id": demand_id_for(canon),
            "counted": counted, **counts}


def no_supply_block(ctx: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The FREE machine-readable answer when exact usable supply is zero:
    what was asked, what exists (counts only — never the paid shortlist,
    trust scores or evidence), and the two zero-cost next actions. Returns
    None when usable supply exists."""
    if not ctx or ctx.get("verified_reachable", 0) > 0:
        return None
    return {
        "no_supply": True,
        "capability": ctx["capability"],
        "demand_id": ctx["demand_id"],
        "supplied": ctx["supplied"],
        "declared_endpoint": ctx["declared_endpoint"],
        "verified_reachable": ctx["verified_reachable"],
        "detail": ("no supplier of this capability currently has a VERIFIED "
                   "reachable endpoint; your demand is recorded (free) under "
                   "the demand_id above"),
        "actions": {
            "demand_watch": {
                "method": "POST", "path": "/demand/watch", "free": True,
                "body": {"capability": ctx["capability"]},
                "note": "attributable standing demand; requires a free "
                        "registration key",
            },
            "register_supplier": {
                "method": "POST", "path": "/agents/register", "free": True,
                "body": {"name": "<you>",
                         "capabilities": [ctx["capability"]]},
                "note": "the first competent supplier of an in-demand "
                        "capability starts at rank 1; declare + verify an "
                        "endpoint to become routable",
            },
        },
        "demand_feed": "/demand/feed",
    }
