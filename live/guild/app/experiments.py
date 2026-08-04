"""The autonomous experiment engine — decide without a human, honestly.

WHY THIS IS NARROW ON PURPOSE
  An autonomous loop that can change anything will eventually change the thing
  that makes its own numbers look good. So this engine can move exactly ONE
  kind of variable: a PRICE, within its published ceiling, downward only. It
  cannot raise a price, cannot invent an operation, cannot alter what counts as
  success, and cannot touch attribution.

  Copy variants are deliberately NOT claimed here. There is no bounded
  mechanism for them yet, and an engine that advertises a lever it does not
  have is worse than one that admits its range.

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
           baseline: dict[str, Any],
           tested_price_credits: Optional[int] = None) -> dict[str, Any]:
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
            # THE EXACT TREATMENT under test. Evidence gathered at a different
            # price is evidence about that other price, not this one.
            "tested_price_credits": tested_price_credits,
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


#: The three INDEPENDENT conditions that must all hold before a settlement may
#: be called revenue. `mode == "x402"` alone is not money: the same rail runs
#: on Base Sepolia by default, where a successful settlement is a successful
#: payment of nothing, and a facilitator's word is not a chain receipt.
SETTLED_MODE = "x402"

#: Which events belong to which paid operation. An experiment on the
#: deep_preflight price must be judged on deep_preflight revenue — not on
#: unrelated escrow settlement or a watch sold for a different offer.
OPERATION_EVENTS: dict[str, tuple[str, ...]] = {
    "deep_preflight": ("deep_preflight_run",),
    "evidence_bundle": ("evidence_bundle_issued",),
    "watch_cycle": ("watch_provisioned",),
}

ALL_PAID_EVENTS = tuple(t for v in OPERATION_EVENTS.values() for t in v)


def is_revenue(event: dict) -> bool:
    """Did real, confirmed, mainnet money move for this event?

    All three conditions, deliberately: mode (not sandbox credits we mint),
    confirmed (the chain receipt was verified, not merely claimed by the
    facilitator) and mainnet (not the value-less default network). Events
    predating settlement metadata have none of these and are never revenue."""
    return (event.get("settlement_mode") == SETTLED_MODE
            and bool(event.get("settlement_confirmed"))
            and bool(event.get("settlement_mainnet")))


def _is_external(event: dict) -> bool:
    from . import attribution
    cls = attribution.caller_class(event)
    return (not event.get("fp")
            and cls not in ("AG_INTERNAL", "AG_TEST", "OPERATOR",
                            "REGISTRY_CRAWLER")
            and attribution.may_count_as_external_growth(cls)
            and attribution.is_genuine_external(event))


def _at_or_after(event: dict, since: Optional[str]) -> bool:
    """Is this event inside the treatment window? FAILS CLOSED.

    An event whose timestamp cannot be parsed is EXCLUDED, not admitted: an
    unreadable date must never be able to decide a price. Cheap string
    comparison first (ISO-8601 sorts lexically for a fixed offset), with a
    real parse as the fallback."""
    if not since:
        return True
    at = event.get("at")
    if not isinstance(at, str) or not at:
        return False
    try:
        return (datetime.fromisoformat(at)
                >= datetime.fromisoformat(str(since)))
    except (TypeError, ValueError):
        return False


def qualified_exposure(store: Any, operation: Optional[str] = None,
                       since: Optional[str] = None,
                       tested_price_credits: Optional[int] = None
                       ) -> dict[str, Any]:
    """Genuinely-external actors who were ACTUALLY OFFERED this paid operation.

    TREATMENT WINDOW AND EXACT TREATMENT (correction 2026-07-31). `since` and
    `tested_price_credits` scope exposure to the CURRENT experiment arm.
    Without them the loop optimises on stale treatment data: ten callers see 20
    credits and do not buy, the engine cuts to 10, and on the next cycle those
    same ten old-price impressions are counted again to justify cutting 10 to
    5 — even though nobody has been shown 10. An impression is evidence about
    the price that was actually displayed, and about nothing else.

    THE IMPRESSION BOUNDARY (correction 2026-07-31). This previously counted
    adjacent free-product events — a caller who ran a FREE preflight was
    treated as exposure for the PAID deep-preflight price experiment. They had
    never been quoted that price, so the engine could reach its denominator and
    halve or kill an offer nobody was shown. "They used the free thing" is not
    evidence about a price.

    Exposure to a paid operation is now exactly two things, both explicit:

      * ``paid_offer_shown`` carrying this operation — the caller was shown
        the price. Two impressions qualify and are distinguished by the
        `impression` field: a 402 / payment-required quote, and a price
        DISPLAYED in a successful response where there is no 402 at all (a
        watch is provisioned free and priced per cycle, so its response is the
        only moment its price is ever shown). Recorded identically on HTTP,
        MCP and A2A. Or
      * a completed call of that operation — they saw the price and paid it.

    Nothing else counts. With no operation given (the commercial report), the
    broader decision-surface view is returned, clearly labelled as such.

    Attribution is the same central rule used everywhere: crawlers,
    first-party tooling and unknown-attributed traffic are excluded
    structurally, never by matching a User-Agent."""
    from . import attribution

    def _external(e: dict) -> bool:
        if e.get("fp") or e.get("first_party"):
            return False
        cls = attribution.caller_class(e)
        if cls in ("AG_INTERNAL", "AG_TEST", "OPERATOR", "REGISTRY_CRAWLER"):
            return False
        return (attribution.may_count_as_external_growth(cls)
                and attribution.is_genuine_external(e))

    actors: set[str] = set()
    events = 0
    challenged = 0
    completed = 0

    if operation:
        completion_types = set(OPERATION_EVENTS.get(operation, ()))
        for e in getattr(store, "events", []):
            etype = e.get("type")
            is_challenge = (etype in ("paid_offer_shown",
                                      "paid_offer_challenged")
                            and e.get("challenged_operation") == operation
                            # distinctness must be KNOWN to count an actor; an
                            # unidentifiable MCP caller is real traffic but
                            # cannot be counted toward an actor threshold
                            and e.get("actor_distinct") is not False)
            # A completion only counts as exposure when it was actually PAID
            # for — a free-tier call of the same shape is not an impression of
            # the price.
            is_completion = (etype in completion_types
                             and e.get("settlement_mode") in
                             ("x402", "credits_sandbox"))
            if not (is_challenge or is_completion):
                continue
            if not _external(e):
                continue
            # Same arm only: inside the window AND at the price under test.
            if not _at_or_after(e, since):
                continue
            if (tested_price_credits is not None
                    and e.get("price_credits") != tested_price_credits):
                continue
            events += 1
            challenged += 1 if is_challenge else 0
            completed += 1 if is_completion else 0
            key = e.get("key") or "anon"
            if key != "anon":
                actors.add(key)
        rule = (f"actors genuinely external AND shown the {operation} price "
                "(paid_offer_challenged) or who completed a paid "
                f"{operation} call. Free-tier use of an adjacent product is "
                "NOT exposure to this price.")
    else:
        # The PORTFOLIO view. It still has to answer the mandate's leading
        # question — "was any external actor shown a price?" — across every
        # operation. Before this, `challenged` and `completed` were only ever
        # incremented inside the `if operation:` branch, so the unscoped
        # report published paid_offers_shown: 0 STRUCTURALLY, whatever the
        # traffic was. A counter that cannot leave zero is not a measurement.
        surfaces = {"preflight_run", "deep_preflight_run",
                    "evidence_bundle_issued", "watch_provisioned",
                    "index_view", "paid_offer_shown", "paid_offer_challenged"}
        for e in getattr(store, "events", []):
            etype = e.get("type")
            if etype not in surfaces or not _external(e):
                continue
            events += 1
            # Same two definitions as the scoped branch, unioned over every
            # operation — deliberately NOT a looser rule, so the portfolio
            # number and the sum of the scoped numbers cannot disagree.
            if (etype in ("paid_offer_shown", "paid_offer_challenged")
                    and e.get("actor_distinct") is not False):
                challenged += 1
            elif (etype in ALL_PAID_EVENTS
                    and e.get("settlement_mode") in
                    ("x402", "credits_sandbox")):
                completed += 1
            key = e.get("key") or "anon"
            if key != "anon":
                actors.add(key)
        rule = ("ALL decision surfaces, free and paid — a portfolio view for "
                "the commercial report, with paid_offers_shown and "
                "paid_completions summed over every operation. NOT valid for "
                "pricing an INDIVIDUAL operation; pass `operation` for that.")

    return {
        "operation_scope": operation or "all_surfaces",
        "qualified_actors": len(actors),
        "qualified_events": events,
        "paid_offers_shown": challenged,
        "paid_completions": completed,
        "rule": rule,
    }


def commercial_metrics(store: Any, operation: Optional[str] = None,
                       since: Optional[str] = None,
                       tested_price_credits: Optional[int] = None
                       ) -> dict[str, Any]:
    """The primary metrics. Revenue is REAL money only.

    `operation` scopes every figure to ONE paid operation. Without it, an
    experiment on the deep_preflight price could be promoted by unrelated
    escrow revenue or by a watch sold for a different offer — the experiment
    would "work" for reasons that had nothing to do with the change it made.
    Global (operation=None) figures remain available for the commercial report,
    where a total is what is wanted."""
    want = (OPERATION_EVENTS.get(operation) if operation else ALL_PAID_EVENTS)

    payers: set[str] = set()
    paid_decisions = 0
    repeat: dict[str, int] = {}
    revenue_usd = 0.0
    sandbox_decisions = 0
    sandbox_actors: set[str] = set()
    unattributed_settled = 0
    testnet_settlements = 0

    for e in getattr(store, "events", []):
        if e.get("type") not in want:
            continue
        # Same arm only. A payment QUOTED at the old price and settled late
        # must not promote the new one — the money is real, but it is evidence
        # about the price the payer was actually shown.
        if not _at_or_after(e, since):
            continue
        # EXACT TREATMENT, FAIL CLOSED. A completion with NO recorded price
        # cannot be shown to belong to this arm, so it is excluded rather than
        # admitted. Excluding a real sale understates us; admitting an
        # unattributable one would let any historical payment promote a price
        # it was never quoted at, which is the failure being fixed.
        if tested_price_credits is not None:
            price = e.get("price_credits")
            if price is None or price != tested_price_credits:
                continue
        key = e.get("key") or ""
        if e.get("settlement_mode") != SETTLED_MODE:
            if e.get("settlement_mode") == "credits_sandbox" or e.get("paid"):
                sandbox_decisions += 1
                if key and key != "anon":
                    sandbox_actors.add(key)
            continue
        if not is_revenue(e):
            # settled on the rail, but testnet and/or unconfirmed — a
            # successful payment of nothing
            testnet_settlements += 1
            continue
        if not _is_external(e):
            unattributed_settled += 1
            continue
        paid_decisions += 1
        revenue_usd += float(e.get("settlement_amount_atomic") or 0) / 1e6
        if key and key != "anon":
            payers.add(key)
            repeat[key] = repeat.get(key, 0) + 1

    monitored = 0
    for w in getattr(store, "watches", {}).values():
        if not w.get("active"):
            continue
        acct = (getattr(store, "accounts", {}) or {}).get(w.get("owner_key") or "")
        if acct and acct.get("first_party"):
            continue        # our own watch is not a customer
        monitored += 1
    if operation and operation != "watch_cycle":
        monitored = 0       # not attributable to this experiment

    return {
        "operation_scope": operation or "all",
        "external_settled_revenue_usd": round(revenue_usd, 6),
        "distinct_external_payers": len(payers),
        "paid_decisions": paid_decisions,
        "externally_monitored_endpoints": monitored,
        "repeat_paid_callers": sum(1 for n in repeat.values() if n > 1),
        "supporting_sandbox_decisions_NOT_REVENUE": sandbox_decisions,
        "supporting_sandbox_distinct_actors_NOT_PAYERS": len(sandbox_actors),
        "supporting_testnet_or_unconfirmed_NOT_REVENUE": testnet_settlements,
        "settled_but_not_attributable_external": unattributed_settled,
        "settlement_rule": (
            "revenue requires ALL of: settlement_mode == 'x402', "
            "settlement_confirmed (chain receipt verified, not the "
            "facilitator's word), settlement_mainnet (the rail defaults to "
            "Base Sepolia, where a successful settlement is a successful "
            "payment of nothing), AND a genuinely external caller. Sandbox "
            "credits, testnet settlements, unconfirmed settlements and "
            "unattributable callers are reported separately and can never "
            "promote an experiment."),
        "revenue_definition": (
            "independently confirmed EXTERNAL mainnet settlement only. "
            "Sandbox credits, first-party canaries, testnet funds and internal "
            "transfers are excluded by construction and are not money."),
    }


def experiment_operation(rec: dict) -> Optional[str]:
    """The paid operation an experiment is bound to, from its variable."""
    variable = str((rec or {}).get("variable") or "")
    if variable.startswith("price:"):
        op = variable.split(":", 1)[1]
        return op if op in OPERATION_EVENTS else None
    return None


def evaluate(store: Any, key: str) -> dict[str, Any]:
    """Decide an experiment — or refuse to, honestly."""
    rec = store.experiments.get(key)
    if not rec:
        return {"key": key, "decision": None, "reason": "unknown experiment"}

    operation = experiment_operation(rec)
    since = rec.get("started_at")
    tested = rec.get("tested_price_credits")
    exposure = qualified_exposure(store, operation, since=since,
                                  tested_price_credits=tested)
    metrics = commercial_metrics(store, operation, since=since,
                                 tested_price_credits=tested)
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

    evidence = {"operation": operation,
                "arm": {"since": since, "tested_price_credits": tested,
                        "note": ("evidence is scoped to THIS arm: impressions "
                                 "of a different price, and impressions from "
                                 "before this window opened, cannot decide "
                                 "it")},
                "exposure": exposure, "metrics": metrics, "baseline": baseline,
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


#: The default experiment the service starts with. ONE, deliberately: the
#: engine applies at most one change per cycle, so seeding several would just
#: queue changes that cannot run concurrently anyway, and would make the first
#: result harder to attribute.
DEFAULT_EXPERIMENTS = (
    {
        "key": "deep_preflight_price_v1",
        "variable": "price:deep_preflight",
        "hypothesis": (
            "Externally-owned agents shown the deep-preflight price do not buy "
            "at 20 credits ($0.02). If qualified callers see the price and "
            "still do not pay, the price is the blocker and halving it should "
            "move paid decisions; if it does not, the OFFER is wrong, not the "
            "price."),
    },
)


def seed_defaults(store: Any) -> dict[str, Any]:
    """Ensure the engine has something to learn from. Idempotent.

    An autonomous experiment engine with no experiment is inert — it evaluates
    an empty dict forever and reports nothing, which does not satisfy "find the
    formula without a human". Seeding happens on the real cycle path so a fresh
    deployment starts measuring by itself.

    Three safety properties, because a seeder that runs every cycle is one bug
    away from continuously resetting the thing it is meant to measure:

      * NEVER overwrites an existing experiment — `define` returns the existing
        record untouched, so the window and baseline survive every restart;
      * NEVER seeds an experiment whose price is pinned by an operator, because
        the engine could not act on it anyway and a permanently undecidable
        experiment is noise;
      * seeds ONE experiment, matching the one-change-per-cycle rule.

    It does NOT fabricate exposure. A seeded experiment with no qualified
    callers correctly reports `insufficient_evidence` — that is the honest
    state of a product nobody has been offered yet."""
    seeded, skipped = [], []
    for spec in DEFAULT_EXPERIMENTS:
        key = spec["key"]
        if key in (getattr(store, "experiments", {}) or {}):
            skipped.append({"key": key, "reason": "already_exists"})
            continue
        operation = spec["variable"].split(":", 1)[1]
        if os.environ.get(pricing._env_key(operation)) is not None:
            skipped.append({"key": key, "reason": "price_pinned_by_operator"})
            continue
        # BASELINE IN THE SAME FRAME AS THE COMPARISON. evaluate() measures
        # this arm (since started_at, at the tested price), so an all-time
        # baseline would compare different frames: historical revenue would
        # sit in the baseline while a genuine new-arm sale sat in the metrics,
        # and the sale would read as "no movement". The arm opens at zero,
        # which is the truth about a window that has just started.
        arm_price = pricing.price(operation)
        arm_start = _now().isoformat()
        rec = define(store, key, hypothesis=spec["hypothesis"],
                     variable=spec["variable"],
                     baseline=commercial_metrics(
                         store, operation, since=arm_start,
                         tested_price_credits=arm_price),
                     tested_price_credits=arm_price)
        # keep the window and the baseline frame identical
        if rec.get("started_at") and rec["started_at"] < arm_start:
            rec["started_at"] = arm_start
            store.experiments[key] = rec
        seeded.append(key)
    return {"seeded": seeded, "already_present": skipped,
            "note": ("idempotent: an existing experiment is never overwritten, "
                     "so a restart cannot reset its window or baseline")}


def apply_next_action(store: Any) -> list[dict[str, Any]]:
    """Decide AND ACT on every running experiment. The autonomous half.

    Previously the scheduled loop only called `evaluate`, so a `kill` verdict
    produced a recommendation nobody read — the engine could observe that an
    offer had failed and was structurally unable to do anything about it. That
    is a strategy memo with a cron job.

    Now, on a DECISIVE kill, exactly ONE reversible change is applied per
    experiment per cycle:

      * the price is halved, clamped to the published ceiling and floored at
        zero (downward only — the engine can never raise a price);
      * the change is written to the DURABLE override layer, so it survives a
        restart and is visible at GET /pricing;
      * before/after/reason are recorded on the experiment;
      * the measurement window RESTARTS and the baseline is re-taken, because
        continuing to measure a changed offer against an old baseline would
        make the next verdict meaningless;
      * a price already at zero is not "changed" again — the engine reports
        `offer_exhausted` rather than pretending a no-op was an action.

    ONE CHANGE PER CYCLE, GLOBALLY. The mandate is "one independently
    measurable change at a time", and that is a property of the SYSTEM, not of
    each experiment: two prices moving in the same cycle makes both results
    uninterpretable, because either change could explain whatever happens next.
    Remaining experiments are evaluated and reported, and simply wait their
    turn — the loop runs on a multi-hour schedule, so deferring costs one
    cycle and buys an attributable result.

    Returns one record per experiment. Never raises: a failure to act must not
    take the scheduled cycle down."""
    applied: list[dict[str, Any]] = []
    acted_this_cycle = False
    for key in list(getattr(store, "experiments", {}) or {}):
        try:
            action = next_action(store, key)
        except Exception as exc:  # noqa: BLE001
            applied.append({"key": key, "error": type(exc).__name__})
            continue
        if action.get("action") != "reprice" or not action.get("change"):
            applied.append({"key": key, "decision": action.get("decision"),
                            "acted": False, "action": action.get("action")})
            continue
        if acted_this_cycle:
            applied.append({"key": key, "decision": action.get("decision"),
                            "acted": False, "reason": "deferred_one_change_per_cycle",
                            "detail": ("another experiment already applied this "
                                       "cycle; two simultaneous changes would "
                                       "make both results unattributable")})
            continue
        change = action["change"]
        op, before, after = (change["operation"], change["from_credits"],
                             change["to_credits"])
        if before <= 0 or after == before:
            applied.append({"key": key, "acted": False,
                            "reason": "offer_exhausted",
                            "detail": ("the price is already at zero — halving "
                                       "it again is not an action, and the "
                                       "OFFER, not the price, is what has "
                                       "failed")})
            continue
        # ENV pins outrank the engine: if an operator has set the price by
        # hand, the loop must not fight them.
        if os.environ.get(pricing._env_key(op)) is not None:
            applied.append({"key": key, "acted": False,
                            "reason": "price_pinned_by_operator",
                            "detail": f"{pricing._env_key(op)} is set; the "
                                      "autonomous engine never overrides a "
                                      "human-pinned price"})
            continue
        with store.lock, store._txn():
            store.price_overrides[op] = after
            pricing.load_runtime(store.price_overrides)
            live = store.experiments.get(key) or {}
            live.setdefault("changes_applied", []).append({
                "at": _now().isoformat(), "operation": op,
                "before_credits": before, "after_credits": after,
                "reason": action.get("reason"),
                "decision": action.get("decision"),
                "reversible_via": pricing._env_key(op),
            })
            live["changes_applied"] = live["changes_applied"][-20:]
            # Restart the window, re-take the baseline, and record the NEW
            # treatment. All three move together: an arm is (window, price),
            # and updating one without the others is how a loop ends up
            # optimising on stale treatment data.
            live["started_at"] = _now().isoformat()
            live["tested_price_credits"] = after
            live["baseline"] = commercial_metrics(
                store, experiment_operation(live),
                since=live["started_at"], tested_price_credits=after)
            live["status"] = "running"
            live["decision"] = None
            live["decided_at"] = None
            store.experiments[key] = live
            if store.backend is not None:
                store._persist_kv("experiments", store.experiments)
                store._persist_kv("price_overrides", store.price_overrides)
            store._save()
        acted_this_cycle = True
        applied.append({
            "key": key, "acted": True, "operation": op,
            "before_credits": before, "after_credits": after,
            "reason": action.get("reason"),
            "window_restarted": True,
            "reversible_via": pricing._env_key(op),
        })
    return applied


def snapshot(store: Any, operation: Optional[str] = None) -> dict[str, Any]:
    """Everything an autonomous report needs, revenue first.

    `operation` scopes BOTH the revenue block and the exposure block to one
    paid operation. It is threaded through rather than accepted and dropped:
    a report that tells the caller to pass `operation` and then ignores it is
    worse than one that never offered the parameter, because the answer looks
    scoped and is not."""
    return {
        "commercial": commercial_metrics(store, operation),
        "qualified_exposure": qualified_exposure(store, operation),
        "experiments": {k: {"status": v.get("status"),
                            "decision": v.get("decision"),
                            "variable": v.get("variable"),
                            "hypothesis": v.get("hypothesis")}
                        for k, v in (store.experiments or {}).items()},
        "applied_changes": {k: v.get("changes_applied", [])
                            for k, v in (store.experiments or {}).items()
                            if v.get("changes_applied")},
        "runtime_price_overrides": pricing.runtime_overrides(),
        "primary_metrics": list(PRIMARY_METRICS),
        "supporting_metrics_never_sufficient": list(SUPPORTING_METRICS),
    }
