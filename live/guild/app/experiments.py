"""The autonomous experiment engine — decide without a human, honestly.

WHY THIS IS NARROW ON PURPOSE
  An autonomous loop that can change anything will eventually change the thing
  that makes its own numbers look good. So this engine can move exactly two
  kinds of variable — a PRICE within its published ceiling, and a COPY variant
  — and nothing else. It cannot alter what counts as success, cannot create an
  operation, and cannot touch attribution.

THE RULE THAT MAKES IT TRUSTWORTHY
  **An experiment can never count our own traffic.** Qualified exposure is
  genuine-external only, through the same central attribution rule used
  everywhere else. First-party tooling, crawlers, registry probes and
  unknown-attributed traffic are excluded structurally, not filtered by name or
  User-Agent. If that leaves a denominator of zero, the verdict is
  ``insufficient_evidence`` — never ``kill``, and never ``promote``.

  This is the difference between an experiment engine and a machine for
  generating flattering conclusions. A "0% conversion on 1,790 crawler
  impressions" verdict is not a finding; it is a category error with a number
  attached.

DECISIONS
  ``promote``               enough qualified exposure AND the metric moved
  ``kill``                  enough qualified exposure AND it did not
  ``hold``                  running, not yet decidable
  ``insufficient_evidence`` the window closed without enough qualified
                            exposure to learn anything — the honest outcome,
                            and the one a vanity dashboard never reports

WHAT COUNTS AS SUCCESS
  Genuine external settled revenue, distinct external payers, paid decisions,
  externally monitored endpoints, repeat paid use. Indexed inventory, free
  checks, page views, crawler reach and passports are SUPPORTING metrics and
  can never promote an experiment on their own.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import pricing

#: Minimum genuinely-external actors before ANY verdict other than
#: insufficient_evidence may be reached. Deliberately small — we are trying to
#: detect the difference between "nobody wants this" and "nobody has seen it",
#: not to run a statistically powered trial.
DEFAULT_MIN_QUALIFIED = 10

#: Hard ceiling on how long an experiment may run before it must conclude.
DEFAULT_WINDOW_DAYS = 14

#: The ONLY metrics that may promote an experiment.
PRIMARY_METRICS = (
    "external_settled_revenue_usd",
    "distinct_external_payers",
    "paid_decisions",
    "externally_monitored_endpoints",
    "repeat_paid_callers",
)

#: Never sufficient on their own, no matter how large.
SUPPORTING_METRICS = (
    "indexed_entries", "free_preflight_runs", "evidence_page_views",
    "crawler_reach", "passports_issued", "offer_served",
)


def min_qualified() -> int:
    try:
        return max(1, min(int(os.environ.get("GUILD_EXP_MIN_QUALIFIED")
                              or DEFAULT_MIN_QUALIFIED), 10_000))
    except (TypeError, ValueError):
        return DEFAULT_MIN_QUALIFIED


def window_days() -> int:
    try:
        return max(1, min(int(os.environ.get("GUILD_EXP_WINDOW_DAYS")
                              or DEFAULT_WINDOW_DAYS), 90))
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_DAYS


def _now() -> datetime:
    return datetime.now(timezone.utc)


def define(store: Any, key: str, *, hypothesis: str, variable: str,
           baseline: dict[str, Any]) -> dict[str, Any]:
    """Register a bounded, reversible experiment. Idempotent by key."""
    with store.lock, store._txn():
        existing = store.experiments.get(key)
        if existing:
            return existing
        rec = {
            "key": key,
            "hypothesis": hypothesis,
            "variable": variable,
            "baseline": baseline,
            "started_at": _now().isoformat(),
            "window_days": window_days(),
            "min_qualified": min_qualified(),
            "status": "running",
            "decision": None,
            "decided_at": None,
            "evidence": None,
        }
        store.experiments[key] = rec
        if store.backend is not None:
            store._persist_kv("experiments", store.experiments)
        store._save()
    return rec


def qualified_exposure(store: Any) -> dict[str, Any]:
    """Genuinely-external actors who reached a decision surface.

    Uses the SAME central attribution rule as every other honest number in the
    service. Crawlers, registry probes, our own tooling and unknown-attributed
    traffic are excluded structurally — never by name-matching a User-Agent,
    which is exactly how self-traffic gets laundered into a growth metric."""
    from . import attribution

    decision_surfaces = {"preflight_run", "deep_preflight_run",
                         "evidence_bundle_issued", "watch_provisioned",
                         "index_view"}
    actors: set[str] = set()
    events = 0
    for e in getattr(store, "events", []):
        if e.get("type") not in decision_surfaces:
            continue
        if e.get("fp") or e.get("first_party"):
            continue
        cls = attribution.caller_class(e)
        if cls in ("AG_INTERNAL", "AG_TEST", "OPERATOR", "REGISTRY_CRAWLER"):
            continue
        if not (attribution.may_count_as_external_growth(cls)
                and attribution.is_genuine_external(e)):
            continue
        events += 1
        key = e.get("key") or "anon"
        if key != "anon":
            actors.add(key)
    return {
        "qualified_actors": len(actors),
        "qualified_events": events,
        "rule": ("genuine-external only, via attribution.caller_class + "
                 "is_genuine_external. Crawlers, first-party tooling and "
                 "unknown-attributed traffic are excluded structurally, not by "
                 "matching a User-Agent string."),
    }


def commercial_metrics(store: Any) -> dict[str, Any]:
    """The primary metrics. Revenue is REAL money only."""
    payers: set[str] = set()
    paid_decisions = 0
    repeat: dict[str, int] = {}
    for e in getattr(store, "events", []):
        if e.get("type") in ("deep_preflight_run", "evidence_bundle_issued") \
                and e.get("paid"):
            paid_decisions += 1
            key = e.get("key") or ""
            if key and key != "anon" and not e.get("fp"):
                payers.add(key)
                repeat[key] = repeat.get(key, 0) + 1

    revenue_usd = 0.0
    try:
        real = (store.escrow_summary() or {}).get("real_settlement") or {}
        revenue_usd = float(
            real.get("independently_attested_external_revenue_usd") or 0.0)
    except Exception:  # noqa: BLE001
        revenue_usd = 0.0

    monitored = 0
    for w in getattr(store, "watches", {}).values():
        if not w.get("active"):
            continue
        acct = (getattr(store, "accounts", {}) or {}).get(w.get("owner_key") or "")
        if acct and acct.get("first_party"):
            continue        # our own watch is not a customer
        monitored += 1

    return {
        "external_settled_revenue_usd": revenue_usd,
        "distinct_external_payers": len(payers),
        "paid_decisions": paid_decisions,
        "externally_monitored_endpoints": monitored,
        "repeat_paid_callers": sum(1 for n in repeat.values() if n > 1),
        "revenue_definition": (
            "independently confirmed EXTERNAL mainnet settlement only. "
            "Sandbox credits, first-party canaries, testnet funds and internal "
            "transfers are excluded by construction and are not money."),
    }


def evaluate(store: Any, key: str) -> dict[str, Any]:
    """Decide an experiment — or refuse to, honestly."""
    rec = store.experiments.get(key)
    if not rec:
        return {"key": key, "decision": None, "reason": "unknown experiment"}

    exposure = qualified_exposure(store)
    metrics = commercial_metrics(store)
    baseline = rec.get("baseline") or {}
    started = rec.get("started_at")
    try:
        elapsed = _now() - datetime.fromisoformat(str(started))
    except (TypeError, ValueError):
        elapsed = timedelta(0)
    expired = elapsed > timedelta(days=int(rec.get("window_days", window_days())))

    moved = any(
        float(metrics.get(m) or 0) > float(baseline.get(m) or 0)
        for m in PRIMARY_METRICS)
    enough = exposure["qualified_actors"] >= int(
        rec.get("min_qualified", min_qualified()))

    if not enough:
        decision = "insufficient_evidence" if expired else "hold"
        reason = (
            f"{exposure['qualified_actors']} qualified external actor(s) — "
            f"below the {rec.get('min_qualified')} needed to tell 'nobody "
            "wants this' apart from 'nobody has seen it'. "
            + ("The window closed without enough exposure to learn anything, "
               "which is a real outcome and is reported as such — not as a "
               "failure of the offer." if expired else "Still gathering."))
    elif moved:
        decision, reason = "promote", (
            "a PRIMARY commercial metric moved against baseline with "
            f"{exposure['qualified_actors']} qualified external actors")
    else:
        decision, reason = "kill", (
            f"{exposure['qualified_actors']} qualified external actors saw it "
            "and no primary commercial metric moved. Supporting metrics "
            "(reach, inventory, free checks) cannot rescue this verdict.")

    evidence = {"exposure": exposure, "metrics": metrics, "baseline": baseline,
                "elapsed_days": round(elapsed.total_seconds() / 86400, 2),
                "window_expired": expired}
    with store.lock, store._txn():
        live = store.experiments.get(key) or rec
        live["decision"] = decision
        live["evidence"] = evidence
        live["status"] = "running" if decision == "hold" else "decided"
        live["decided_at"] = None if decision == "hold" else _now().isoformat()
        store.experiments[key] = live
        if store.backend is not None:
            store._persist_kv("experiments", store.experiments)
        store._save()
    return {"key": key, "decision": decision, "reason": reason,
            "evidence": evidence}


def next_action(store: Any, key: str) -> dict[str, Any]:
    """The single safest reversible next move, chosen without a human.

    On `kill` the response is to CHANGE THE OFFER — lower the price one step
    within its published ceiling — not to keep running and keep reporting
    reach. The mandate is explicit: if no genuine paid demand appears after the
    documented threshold, change the offer or price rather than celebrating
    reach."""
    verdict = evaluate(store, key)
    rec = store.experiments.get(key) or {}
    variable = rec.get("variable") or ""
    decision = verdict["decision"]

    if decision == "promote":
        return {**verdict, "action": "keep", "change": None,
                "rationale": "it is working; changing it now would destroy the "
                             "only signal we have"}
    if decision == "hold":
        return {**verdict, "action": "wait", "change": None,
                "rationale": "not yet decidable on qualified exposure"}
    if decision == "insufficient_evidence":
        return {**verdict, "action": "increase_qualified_exposure",
                "change": None,
                "rationale": ("the offer was never actually tested. Fix "
                              "DISTRIBUTION to qualified callers before "
                              "touching the price — repricing something nobody "
                              "saw teaches us nothing")}

    # decision == kill → move the price one step down, within the ceiling
    if variable.startswith("price:"):
        op = variable.split(":", 1)[1]
        current = pricing.price(op)
        proposed = max(0, int(current * 0.5))
        return {**verdict, "action": "reprice",
                "change": {"operation": op, "from_credits": current,
                           "to_credits": proposed,
                           "env": pricing._env_key(op),
                           "reversible": True,
                           "within_ceiling": proposed <= pricing.CEILINGS.get(op, 0)},
                "rationale": ("qualified callers saw the offer and did not buy. "
                              "Halve the price within its published ceiling and "
                              "re-run — this is a config change and a rollback, "
                              "not a deploy")}
    return {**verdict, "action": "change_offer", "change": None,
            "rationale": "qualified callers saw it and did not buy; the offer "
                         "itself needs to change"}


def snapshot(store: Any) -> dict[str, Any]:
    """Everything an autonomous report needs, revenue first."""
    return {
        "commercial": commercial_metrics(store),
        "qualified_exposure": qualified_exposure(store),
        "experiments": {k: {"status": v.get("status"),
                            "decision": v.get("decision"),
                            "variable": v.get("variable"),
                            "hypothesis": v.get("hypothesis")}
                        for k, v in (store.experiments or {}).items()},
        "primary_metrics": list(PRIMARY_METRICS),
        "supporting_metrics_never_sufficient": list(SUPPORTING_METRICS),
    }
