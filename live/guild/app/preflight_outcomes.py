"""AGPO-1 — preflight outcome records: the one complete evidence → action →
outcome path Agent Guild instruments end to end.

Why this exists (founder direction 2026-09-22, docs/DIRECTION_2026-09-22.md):
the objective is to demonstrate that *independently operated* agents make
better decisions using AG's evidence, with measured benefits AND measured
mistakes. Until now every free preflight ended at the verdict; nothing
recorded what the agent did next or what happened. This module closes that
loop for exactly one decision — "should I call / delegate to this endpoint
right now?" — and nothing else.

Honesty rules this module enforces by construction:

* A verdict is stamped with an opaque `preflight_id`, its observation time and
  the probe latency, so an outcome can only ever be joined to the exact
  evidence the agent saw. No id, no join; no join, no claim.
* An outcome is what the REPORTER says happened. `reporter_kind` separates an
  automatic integration hook (`integration` → observed action) from an agent's
  own account (`agent` → participant report). Our classification of that
  outcome is labelled as OUR INFERENCE and the rule that produced it is
  published with every summary.
* A skipped call whose counterfactual was not checked is
  `counterfactual_unobserved` — never a benefit. Only a reporter-supplied
  baseline (`baseline.direct_check`) can turn a skip into an avoided failure
  or an unnecessary refusal.
* Runs with no outcome are reported as UNKNOWN, never as refusal or success.
* Independent, first-party, tooling/crawler and propagation records are
  summarised separately and only the independent bucket can ever satisfy the
  objective. The headline flag is literal: `independently_demonstrated_benefit`
  is False until an EXTERNAL_* actor's record classifies as a benefit.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from . import attribution

SCHEMA = "AGPO-1/1.0"

ACTIONS = ("called", "delegated", "declined", "skipped")
OBSERVED = ("success", "failure", "unknown")
DETAILS = ("ok", "timeout", "unreachable", "http_4xx", "http_5xx",
           "protocol_error", "payment_mismatch", "wrong_result", "other",
           "not_applicable")
BASELINE_CHECKS = ("success", "failure", "not_run")
REPORTER_KINDS = ("integration", "agent")
VERDICTS = ("no_failed_checks", "delegate_with_caution", "do_not_delegate")

# Classification vocabulary. Benefits and mistakes are both first-class.
BENEFITS = ("avoided_broken_endpoint",)
MISTAKES = ("unnecessary_refusal", "missed_problem")
NEUTRAL = ("no_difference", "evidence_correct_not_followed",
           "caution_borne_out", "counterfactual_unobserved", "unknown")

RULES: tuple[dict[str, Any], ...] = (
    # --- the agent did NOT call the endpoint ---------------------------------
    {"when": "action in (declined, skipped) and baseline.direct_check == failure",
     "result": "avoided_broken_endpoint", "kind": "benefit",
     "checked_by": "reporter's own direct check of the endpoint"},
    {"when": "action in (declined, skipped) and baseline.direct_check == success",
     "result": "unnecessary_refusal", "kind": "mistake",
     "checked_by": "reporter's own direct check of the endpoint"},
    {"when": "action in (declined, skipped) and baseline.direct_check == not_run",
     "result": "counterfactual_unobserved", "kind": "unknown",
     "checked_by": "nothing — the endpoint was never exercised"},
    # --- the agent DID call the endpoint --------------------------------------
    {"when": "action in (called, delegated) and observed == failure and verdict == do_not_delegate",
     "result": "evidence_correct_not_followed", "kind": "neutral",
     "checked_by": "reporter observed the failure AG predicted"},
    {"when": "action in (called, delegated) and observed == failure and verdict == delegate_with_caution",
     "result": "caution_borne_out", "kind": "neutral",
     "checked_by": "reporter observed a failure after a caution verdict"},
    {"when": "action in (called, delegated) and observed == failure and verdict == no_failed_checks",
     "result": "missed_problem", "kind": "mistake",
     "checked_by": "reporter observed a failure AG's checks did not predict"},
    {"when": "action in (called, delegated) and observed == success and verdict == do_not_delegate",
     "result": "unnecessary_refusal", "kind": "mistake",
     "checked_by": "reporter observed success where AG advised against calling"},
    {"when": "action in (called, delegated) and observed == success",
     "result": "no_difference", "kind": "neutral",
     "checked_by": "the call succeeded; AG's evidence did not change the action"},
    {"when": "observed == unknown", "result": "unknown", "kind": "unknown",
     "checked_by": "nothing"},
)

INDEPENDENT = ("EXTERNAL_UNKNOWN", "EXTERNAL_MEMBER", "EXTERNAL_VERIFIED")
FIRST_PARTY = ("AG_INTERNAL", "AG_TEST", "OPERATOR")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return "pf_" + uuid.uuid4().hex[:20]


def stamp(out: dict[str, Any], *, started: float) -> dict[str, Any]:
    """Attach the evidence identity a later outcome must cite."""
    out["preflight_id"] = new_id()
    out["observed_at"] = _now_iso()
    out["probe_latency_ms"] = int((time.monotonic() - started) * 1000)
    out["freshness"] = {
        "kind": "live_probe",
        "note": ("this is the endpoint's state at observed_at; the check is "
                 "not cached and does not predict later behaviour — re-run "
                 "before acting if minutes have passed"),
    }
    out["outcome_report"] = {
        "schema": SCHEMA,
        "purpose": ("optional, free, no key: tell the Guild what you did after "
                    "this verdict and what happened, so benefits AND mistakes "
                    "of this check are measured instead of assumed"),
        "call": {"method": "POST", "path": "/preflight/outcome"},
        "body": {"preflight_id": out["preflight_id"],
                 "action": list(ACTIONS), "observed": list(OBSERVED),
                 "detail?": list(DETAILS),
                 "baseline?": {"direct_check": list(BASELINE_CHECKS)},
                 "overhead_ms?": "int", "reporter_kind": list(REPORTER_KINDS)},
        "requires_local_authorisation": False,
        "effect": "write (one small record; no identity, no payload)",
    }
    return out


class OutcomeError(ValueError):
    pass


def validate(body: Mapping[str, Any]) -> dict[str, Any]:
    pid = str(body.get("preflight_id") or "")
    if not (pid.startswith("pf_") and 8 <= len(pid) <= 40 and pid[3:].isalnum()):
        raise OutcomeError("preflight_id must be the id returned by /preflight")
    action = str(body.get("action") or "")
    if action not in ACTIONS:
        raise OutcomeError(f"action must be one of {ACTIONS}")
    observed = str(body.get("observed") or "unknown")
    if observed not in OBSERVED:
        raise OutcomeError(f"observed must be one of {OBSERVED}")
    if action in ("declined", "skipped") and observed != "unknown":
        raise OutcomeError("observed must be 'unknown' when the endpoint was "
                           "not called; use baseline.direct_check instead")
    detail = str(body.get("detail") or "not_applicable")
    if detail not in DETAILS:
        raise OutcomeError(f"detail must be one of {DETAILS}")
    baseline = body.get("baseline") or {}
    if not isinstance(baseline, Mapping):
        raise OutcomeError("baseline must be an object")
    direct = str(baseline.get("direct_check") or "not_run")
    if direct not in BASELINE_CHECKS:
        raise OutcomeError(f"baseline.direct_check must be one of {BASELINE_CHECKS}")
    kind = str(body.get("reporter_kind") or "")
    if kind not in REPORTER_KINDS:
        raise OutcomeError(f"reporter_kind must be one of {REPORTER_KINDS}")
    overhead = body.get("overhead_ms")
    if overhead is not None:
        try:
            overhead = max(0, min(int(overhead), 3_600_000))
        except (TypeError, ValueError):
            raise OutcomeError("overhead_ms must be an integer") from None
    return {"preflight_id": pid, "action": action, "observed": observed,
            "detail": detail, "baseline_direct_check": direct,
            "reporter_kind": kind, "overhead_ms": overhead}


def classify(verdict: Optional[str], rec: Mapping[str, Any]) -> dict[str, str]:
    """OUR INFERENCE from the reporter's facts. Pure; the rule table above is
    the specification and is published with every summary."""
    action, observed = rec["action"], rec["observed"]
    direct = rec.get("baseline_direct_check", "not_run")
    if action in ("declined", "skipped"):
        if direct == "failure":
            return {"result": "avoided_broken_endpoint", "kind": "benefit"}
        if direct == "success":
            return {"result": "unnecessary_refusal", "kind": "mistake"}
        return {"result": "counterfactual_unobserved", "kind": "unknown"}
    if observed == "failure":
        if verdict == "do_not_delegate":
            return {"result": "evidence_correct_not_followed", "kind": "neutral"}
        if verdict == "delegate_with_caution":
            return {"result": "caution_borne_out", "kind": "neutral"}
        if verdict == "no_failed_checks":
            return {"result": "missed_problem", "kind": "mistake"}
        return {"result": "unknown", "kind": "unknown"}
    if observed == "success":
        if verdict == "do_not_delegate":
            return {"result": "unnecessary_refusal", "kind": "mistake"}
        if verdict in ("no_failed_checks", "delegate_with_caution"):
            return {"result": "no_difference", "kind": "neutral"}
        return {"result": "unknown", "kind": "unknown"}
    return {"result": "unknown", "kind": "unknown"}


def _bucket(event: Mapping[str, Any]) -> str:
    """Independence is the SAME gate the rest of the Guild uses
    (attribution.is_genuine_external): an EXTERNAL_* class AND an identity
    that is not bare tooling, not a crawler, not an AG-distributed client.
    Bare curl/urllib is indistinguishable from our own traffic and lands in
    tooling_or_crawler, never in independent."""
    cls = attribution.caller_class(event)
    if cls in FIRST_PARTY:
        return "first_party"
    if cls == "PROPAGATION_CLIENT":
        return "propagation_client"
    if cls in INDEPENDENT and attribution.is_genuine_external(
            dict(event), classified=cls):
        return "independent"
    return "tooling_or_crawler"


def _empty_bucket() -> dict[str, Any]:
    return {"outcomes": 0, "benefit": 0, "mistake": 0, "neutral": 0,
            "unknown": 0, "by_result": {}, "by_verdict": {},
            "by_evidence_label": {"observed_action": 0,
                                  "participant_report": 0},
            "runs_stamped": 0, "runs_without_outcome": 0,
            "median_probe_latency_ms": None,
            "median_reported_overhead_ms": None}


def _median(xs: list[int]) -> Optional[int]:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[len(xs) // 2]


def summary(store: Any) -> dict[str, Any]:
    events, coverage = store.measurement_event_snapshot(
        types=("preflight_run", "preflight_outcome"))
    runs: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.get("type") == "preflight_run" and e.get("preflight_id"):
            runs[str(e["preflight_id"])] = e
    buckets = {k: _empty_bucket() for k in
               ("independent", "first_party", "propagation_client",
                "tooling_or_crawler")}
    latencies: dict[str, list[int]] = {k: [] for k in buckets}
    overheads: dict[str, list[int]] = {k: [] for k in buckets}
    joined: set[str] = set()
    unjoined_outcomes = 0
    for e in events:
        if e.get("type") != "preflight_outcome":
            continue
        pid = str(e.get("preflight_id") or "")
        run = runs.get(pid)
        if run is None:
            unjoined_outcomes += 1
            continue
        joined.add(pid)
        # Independence requires BOTH the evidence request and the outcome
        # report to be non-first-party; the stricter bucket wins.
        b_out, b_run = _bucket(e), _bucket(run)
        order = ("first_party", "tooling_or_crawler", "propagation_client",
                 "independent")
        bucket = min((b_out, b_run), key=order.index)
        bk = buckets[bucket]
        c = classify(run.get("verdict"), e)
        bk["outcomes"] += 1
        bk[c["kind"]] += 1
        bk["by_result"][c["result"]] = bk["by_result"].get(c["result"], 0) + 1
        v = str(run.get("verdict") or "unknown")
        bk["by_verdict"][v] = bk["by_verdict"].get(v, 0) + 1
        label = ("observed_action" if e.get("reporter_kind") == "integration"
                 else "participant_report")
        bk["by_evidence_label"][label] += 1
        if e.get("overhead_ms") is not None:
            overheads[bucket].append(int(e["overhead_ms"]))
    for pid, run in runs.items():
        bucket = _bucket(run)
        buckets[bucket]["runs_stamped"] += 1
        if pid not in joined:
            buckets[bucket]["runs_without_outcome"] += 1
        if run.get("probe_latency_ms") is not None:
            latencies[bucket].append(int(run["probe_latency_ms"]))
    for k, bk in buckets.items():
        bk["median_probe_latency_ms"] = _median(latencies[k])
        bk["median_reported_overhead_ms"] = _median(overheads[k])
    ind = buckets["independent"]
    demonstrated = ind["benefit"] > 0
    return {
        "schema": SCHEMA,
        "question": "How did an independently operated agent benefit from "
                    "AG's preflight evidence?",
        "independently_demonstrated_benefit": demonstrated,
        "headline": (
            f"{ind['benefit']} independent benefit record(s), "
            f"{ind['mistake']} independent mistake record(s)"
            if demonstrated else
            "No independently demonstrated benefit yet."),
        "by_actor_class": buckets,
        "unjoined_outcome_reports": unjoined_outcomes,
        "reading_rules": {
            "independent": "EXTERNAL_* callers on BOTH the preflight and the "
                           "outcome report; only this bucket can satisfy the "
                           "objective",
            "first_party": "AG's own agents, tests and operator — evidence of "
                           "capability, never of adoption",
            "runs_without_outcome": "UNKNOWN — a run with no report is neither "
                                    "a refusal nor a success",
            "counterfactual_unobserved": "a skipped call with no direct check "
                                         "is not a benefit",
            "evidence_labels": "observed_action = automatic integration hook; "
                               "participant_report = the agent's own account; "
                               "every result above is OUR INFERENCE from those "
                               "facts via `rules`",
            "payment": "irrelevant here; the path is free and payment proves "
                       "nothing about value",
        },
        "rules": list(RULES),
        "measurement_coverage": coverage,
    }
