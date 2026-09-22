"""AGPO-1 — preflight outcome records: the one complete evidence → action →
outcome path Agent Guild instruments end to end.

Why this exists (founder direction 2026-09-22, docs/DIRECTION_2026-09-22.md):
the objective is to demonstrate that *independently operated* agents make
better decisions using AG's evidence, with measured benefits AND measured
mistakes. A free preflight used to end at the verdict; this module records
what the agent did next, what happened, and — separately — whether AG's
evidence can honestly be credited.

Revised 2026-09-22 after an independent review (Codex) of the first cut,
which found four ways the first version could manufacture or block a claim.
The rules below are the corrections; the review is cited in the tests.

Honesty rules enforced by construction:

* A verdict is stamped with an opaque `preflight_id`, its observation time,
  the probe latency and the NAMES of its failed / unknown checks. An outcome
  joins only to that exact evidence. No id, no join; no join, no claim.
* Three things are kept apart and never collapsed: (1) whether AG's checks
  were CORRECT about the endpoint, (2) whether the reporter says AG's
  evidence INFLUENCED the decision (`decision_basis`, a claim), and
  (3) whether a benefit is INDEPENDENTLY SUPPORTED (a reporter-run direct
  check confirming the specific problem AG warned about). Only all three
  together produce `avoided_broken_endpoint`. AG being right while the agent
  decided on its own check is `correct_warning_independently_confirmed`,
  never a benefit.
* A skipped call with no direct check is `counterfactual_unobserved`.
* An outcome must match the SCOPE of the checks: a successful call does not
  contradict an identity or payment warning; a failure AG never claimed to
  test is `failure_out_of_scope`, not a missed problem.
* One decision per `preflight_id`. Repeated identical reports are ignored; a
  later different report is a correction (visible as such). Report events
  and evaluable decisions are counted separately.
* `reporter_kind` and `decision_basis` are CLAIMS by the reporter and are
  labelled as claims. Nothing here verifies them.
* Ownership is separate from client type. A User-Agent never establishes an
  independent operator. Only a registered participant presenting its own key
  on BOTH the preflight and the outcome report is `registered_participant`;
  framework-looking traffic is `heuristic_external`; AG-distributed clients
  are `propagation_client`; our own traffic is `first_party`. The headline is
  literally "No independently demonstrated benefit yet." until a
  registered_participant decision classifies as a benefit.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from . import attribution

SCHEMA = "AGPO-1/1.1"

ACTIONS = ("called", "delegated", "declined", "skipped")
OBSERVED = ("success", "failure", "unknown")
DETAILS = ("ok", "timeout", "unreachable", "http_4xx", "http_5xx",
           "protocol_error", "payment_mismatch", "wrong_result", "other",
           "not_applicable")
BASELINE_CHECKS = ("success", "failure", "not_run")
REPORTER_KINDS = ("integration", "agent")
DECISION_BASIS = ("ag_evidence", "own_check", "both", "other", "unknown")
VERDICTS = ("no_failed_checks", "delegate_with_caution", "do_not_delegate")
WARNING_VERDICTS = ("delegate_with_caution", "do_not_delegate")

# A reporter's coarse failure detail maps onto AT MOST one preflight check.
# Anything else is out of the verdict's scope and cannot count against it.
DETAIL_TO_CHECK = {
    "timeout": "endpoint_reachable",
    "unreachable": "endpoint_reachable",
    "http_5xx": "endpoint_reachable",
    "protocol_error": "protocol_handshake",
    "payment_mismatch": "payment_claim_holds",
}
# Checks that a SUCCESSFUL call (or successful direct check) contradicts.
# Identity / evidence / payment-claim checks are not contradicted by a call
# that happened to work.
CONTRADICTED_BY_SUCCESS = frozenset({"endpoint_reachable", "protocol_handshake"})

# Reports older than this relative to the evidence are not joined as a
# decision outcome: the evidence was a live probe, not a prediction.
MAX_JOIN_AGE_S = 24 * 3600

RESULT_KINDS = {
    "avoided_broken_endpoint": "benefit",
    "unnecessary_refusal": "mistake",
    "missed_problem": "mistake",
    "correct_warning_independently_confirmed": "neutral",
    "correct_warning_influence_unknown": "unresolved",
    "warning_not_contradicted": "neutral",
    "evidence_correct_not_followed": "neutral",
    "unknown_check_not_covered": "neutral",
    "consistent_outcome": "neutral",
    "consistent_no_action": "neutral",
    "failure_out_of_scope": "unresolved",
    "counterfactual_unobserved": "unknown",
    "stale_join": "unresolved",
    "unknown": "unknown",
}

RULES: tuple[dict[str, str], ...] = (
    {"when": "not called; direct_check == failure; verdict is a warning; decision_basis in (ag_evidence, both)",
     "result": "avoided_broken_endpoint",
     "checked_by": "reporter's own direct check confirmed a problem AG warned about, and the reporter says AG's evidence drove the decision (a claim)"},
    {"when": "not called; direct_check == failure; verdict is a warning; decision_basis == own_check",
     "result": "correct_warning_independently_confirmed",
     "checked_by": "AG was right, but the reporter decided on its own check — no benefit attributable to AG"},
    {"when": "not called; direct_check == failure; verdict is a warning; decision_basis in (other, unknown)",
     "result": "correct_warning_influence_unknown",
     "checked_by": "AG was right; whether it influenced the decision was not reported"},
    {"when": "not called; direct_check == failure; verdict == no_failed_checks",
     "result": "missed_problem",
     "checked_by": "reporter's own direct check found a problem AG's checks passed"},
    {"when": "not called; direct_check == success; verdict is a warning; every failed check is one a working call contradicts",
     "result": "unnecessary_refusal",
     "checked_by": "reporter's own direct check succeeded where AG reported reachability/handshake failures"},
    {"when": "not called; direct_check == success; verdict is a warning; some failed check is identity/payment/evidence",
     "result": "warning_not_contradicted",
     "checked_by": "a working call does not test the check AG failed"},
    {"when": "not called; direct_check == success; verdict == no_failed_checks",
     "result": "consistent_no_action",
     "checked_by": "AG and the direct check agreed; the agent did not call anyway"},
    {"when": "not called; direct_check == not_run",
     "result": "counterfactual_unobserved",
     "checked_by": "nothing — the endpoint was never exercised"},
    {"when": "called; observed == failure; detail maps to a check AG scored as proven",
     "result": "missed_problem",
     "checked_by": "reporter observed a failure in a check AG said passed"},
    {"when": "called; observed == failure; detail maps to a check AG reported FAILED",
     "result": "evidence_correct_not_followed",
     "checked_by": "reporter observed the failure AG predicted"},
    {"when": "called; observed == failure; detail maps to a check AG reported UNKNOWN",
     "result": "unknown_check_not_covered",
     "checked_by": "AG said it could not test this; the failure is not a missed problem"},
    {"when": "called; observed == failure; detail maps to no preflight check",
     "result": "failure_out_of_scope",
     "checked_by": "nothing — the verdict made no claim about this failure"},
    {"when": "called; observed == success; verdict is a warning; every failed check is one a working call contradicts",
     "result": "unnecessary_refusal",
     "checked_by": "reporter observed success where AG reported reachability/handshake failures"},
    {"when": "called; observed == success; verdict is a warning; some failed check is identity/payment/evidence",
     "result": "warning_not_contradicted",
     "checked_by": "a working call does not test the check AG failed"},
    {"when": "called; observed == success; verdict == no_failed_checks",
     "result": "consistent_outcome",
     "checked_by": "the call worked and AG had said the checks passed; this is consistency, not demonstrated benefit"},
    {"when": "report more than 24h after the evidence",
     "result": "stale_join",
     "checked_by": "nothing — a live probe does not predict the endpoint a day later"},
    {"when": "observed == unknown, or no rule matched",
     "result": "unknown", "checked_by": "nothing"},
)

OWNERSHIPS = ("first_party", "registered_participant", "heuristic_external",
              "propagation_client", "tooling_or_crawler", "unknown")
FIRST_PARTY_CLASSES = ("AG_INTERNAL", "AG_TEST", "OPERATOR")


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
        "purpose": ("optional, free: tell the Guild what you did after this "
                    "verdict and what happened, so benefits AND mistakes of "
                    "this check are measured instead of assumed"),
        "call": {"method": "POST", "path": "/preflight/outcome"},
        "body": {"preflight_id": out["preflight_id"],
                 "action": list(ACTIONS), "observed": list(OBSERVED),
                 "detail?": list(DETAILS),
                 "baseline?": {"direct_check": list(BASELINE_CHECKS)},
                 "decision_basis?": list(DECISION_BASIS),
                 "overhead_ms?": "int", "reporter_kind": list(REPORTER_KINDS)},
        "provenance": ("present the same X-API-Key on this call and on the "
                       "report to be counted as a registered participant; "
                       "without it your report is classed by User-Agent "
                       "heuristics and can never establish independence"),
        "requires_local_authorisation": False,
        "effect": "write (one small record; no payload)",
    }
    return out


def run_event_fields(out: Mapping[str, Any]) -> dict[str, Any]:
    """What the preflight_run event must carry for a later join."""
    return {
        "preflight_id": out["preflight_id"],
        "probe_latency_ms": out["probe_latency_ms"],
        "failed_checks": list(out.get("failed") or []),
        "unknown_checks": list(out.get("unknowns") or []),
        "scored_checks": list(out.get("scored") or []),
    }


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
    basis = str(body.get("decision_basis") or "unknown")
    if basis not in DECISION_BASIS:
        raise OutcomeError(f"decision_basis must be one of {DECISION_BASIS}")
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
            "decision_basis": basis, "reporter_kind": kind,
            "overhead_ms": overhead}


# ---------------------------------------------------------------------------
# classification — OUR INFERENCE from the reporter's claims + AG's own record
# ---------------------------------------------------------------------------

def _as_list(v: Any) -> list[str]:
    return [str(x) for x in (v or [])] if isinstance(v, (list, tuple)) else []


def classify(run: Mapping[str, Any], rec: Mapping[str, Any]) -> dict[str, str]:
    """Pure. `run` is the preflight_run event (verdict + check names);
    `rec` the outcome record. The rule table above is the specification."""
    verdict = run.get("verdict")
    failed = set(_as_list(run.get("failed_checks")))
    unknown = set(_as_list(run.get("unknown_checks")))
    scored = set(_as_list(run.get("scored_checks")))
    proven = scored - failed
    action, observed = rec["action"], rec["observed"]
    direct = rec.get("baseline_direct_check", "not_run")
    basis = rec.get("decision_basis", "unknown")
    warning = verdict in WARNING_VERDICTS
    clean = verdict == "no_failed_checks"

    def r(result: str) -> dict[str, str]:
        return {"result": result, "kind": RESULT_KINDS[result]}

    if action in ("declined", "skipped"):
        if direct == "failure":
            if warning:
                if basis in ("ag_evidence", "both"):
                    return r("avoided_broken_endpoint")
                if basis == "own_check":
                    return r("correct_warning_independently_confirmed")
                return r("correct_warning_influence_unknown")
            if clean:
                return r("missed_problem")
            return r("unknown")
        if direct == "success":
            if warning:
                if failed and failed <= CONTRADICTED_BY_SUCCESS:
                    return r("unnecessary_refusal")
                return r("warning_not_contradicted")
            if clean:
                return r("consistent_no_action")
            return r("unknown")
        return r("counterfactual_unobserved")

    # called / delegated
    if observed == "failure":
        check = DETAIL_TO_CHECK.get(rec.get("detail", ""))
        if check is None:
            return r("failure_out_of_scope")
        if check in failed:
            return r("evidence_correct_not_followed")
        if check in unknown:
            return r("unknown_check_not_covered")
        if check in proven:
            return r("missed_problem")
        return r("failure_out_of_scope")
    if observed == "success":
        if warning:
            if failed and failed <= CONTRADICTED_BY_SUCCESS:
                return r("unnecessary_refusal")
            return r("warning_not_contradicted")
        if clean:
            return r("consistent_outcome")
        return r("unknown")
    return r("unknown")


# ---------------------------------------------------------------------------
# ownership (who) and client type (what) — kept apart
# ---------------------------------------------------------------------------

def client_type(event: Mapping[str, Any]) -> str:
    ua = str(event.get("ua") or "")
    if attribution.PROPAGATION_UA_RE.search(ua):
        return "ag_distributed_client"
    if ua.startswith("mcp:"):
        return "mcp_client"
    if attribution.TOOLING_UA_RE.search(ua):
        return "tooling"
    if attribution.FRAMEWORK_RE.search(ua):
        return "framework"
    return "other"


def ownership(event: Mapping[str, Any]) -> str:
    cls = attribution.caller_class(event)
    if cls in FIRST_PARTY_CLASSES or event.get("participant_first_party"):
        return "first_party"
    if event.get("participant_agent_id"):
        return "registered_participant"
    if cls == "PROPAGATION_CLIENT":
        return "propagation_client"
    if cls == "REGISTRY_CRAWLER":
        return "tooling_or_crawler"
    if attribution.is_genuine_external(dict(event), classified=cls):
        return "heuristic_external"
    if client_type(event) == "tooling":
        return "tooling_or_crawler"
    return "unknown"


_OWNERSHIP_STRICTNESS = ("first_party", "tooling_or_crawler", "unknown",
                         "propagation_client", "heuristic_external",
                         "registered_participant")


def joint_ownership(run: Mapping[str, Any], rec: Mapping[str, Any]) -> str:
    """Both legs must agree; the weaker leg wins. A registered participant
    counts only when the SAME agent id is on the preflight and the report."""
    a, b = ownership(run), ownership(rec)
    if "registered_participant" in (a, b):
        same = (run.get("participant_agent_id")
                and run.get("participant_agent_id") == rec.get("participant_agent_id"))
        if not same:
            a = "unknown" if a == "registered_participant" else a
            b = "unknown" if b == "registered_participant" else b
    return min((a, b), key=_OWNERSHIP_STRICTNESS.index)


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------

def _empty_bucket() -> dict[str, Any]:
    return {"decisions": 0, "report_events": 0, "corrected_decisions": 0,
            "benefit": 0, "mistake": 0, "neutral": 0, "unresolved": 0,
            "unknown": 0, "by_result": {}, "by_verdict": {},
            "by_client_type": {},
            "by_reporter_claim": {"claimed_integration_hook": 0,
                                  "participant_report": 0},
            "by_decision_basis_claim": {},
            "runs_stamped": 0, "runs_without_outcome": 0,
            "median_probe_latency_ms": None,
            "median_reported_overhead_ms": None}


def _median(xs: list[int]) -> Optional[int]:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[len(xs) // 2]


def _ts(e: Mapping[str, Any]) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(e.get("at"))).timestamp()
    except (TypeError, ValueError):
        return None


_DECISION_FIELDS = ("action", "observed", "detail", "baseline_direct_check",
                    "decision_basis")


def summary(store: Any) -> dict[str, Any]:
    events, coverage = store.measurement_event_snapshot(
        types=("preflight_run", "preflight_outcome"))
    runs: dict[str, dict[str, Any]] = {}
    reports: dict[str, list[dict[str, Any]]] = {}
    unjoined = 0
    for e in events:
        pid = str(e.get("preflight_id") or "")
        if e.get("type") == "preflight_run" and pid:
            runs[pid] = e
        elif e.get("type") == "preflight_outcome":
            reports.setdefault(pid, []).append(e)
    buckets = {k: _empty_bucket() for k in OWNERSHIPS}
    latencies: dict[str, list[int]] = {k: [] for k in OWNERSHIPS}
    overheads: dict[str, list[int]] = {k: [] for k in OWNERSHIPS}
    decided: set[str] = set()
    for pid, evs in reports.items():
        run = runs.get(pid)
        if run is None:
            unjoined += len(evs)
            continue
        evs = sorted(evs, key=lambda e: (_ts(e) or 0.0))
        latest = evs[-1]
        distinct = {tuple(str(e.get(f)) for f in _DECISION_FIELDS) for e in evs}
        own = joint_ownership(run, latest)
        bk = buckets[own]
        bk["report_events"] += len(evs)
        bk["decisions"] += 1
        decided.add(pid)
        if len(distinct) > 1:
            bk["corrected_decisions"] += 1
        run_t, rep_t = _ts(run), _ts(latest)
        if run_t is not None and rep_t is not None and rep_t - run_t > MAX_JOIN_AGE_S:
            c = {"result": "stale_join", "kind": RESULT_KINDS["stale_join"]}
        else:
            c = classify(run, latest)
        bk[c["kind"]] += 1
        bk["by_result"][c["result"]] = bk["by_result"].get(c["result"], 0) + 1
        v = str(run.get("verdict") or "unknown")
        bk["by_verdict"][v] = bk["by_verdict"].get(v, 0) + 1
        ct = client_type(latest)
        bk["by_client_type"][ct] = bk["by_client_type"].get(ct, 0) + 1
        claim = ("claimed_integration_hook"
                 if latest.get("reporter_kind") == "integration"
                 else "participant_report")
        bk["by_reporter_claim"][claim] += 1
        basis = str(latest.get("decision_basis") or "unknown")
        bk["by_decision_basis_claim"][basis] = bk["by_decision_basis_claim"].get(basis, 0) + 1
        if latest.get("overhead_ms") is not None:
            overheads[own].append(int(latest["overhead_ms"]))
    for pid, run in runs.items():
        own = ownership(run)
        buckets[own]["runs_stamped"] += 1
        if pid not in decided:
            buckets[own]["runs_without_outcome"] += 1
        if run.get("probe_latency_ms") is not None:
            latencies[own].append(int(run["probe_latency_ms"]))
    for k, bk in buckets.items():
        bk["median_probe_latency_ms"] = _median(latencies[k])
        bk["median_reported_overhead_ms"] = _median(overheads[k])
    reg = buckets["registered_participant"]
    demonstrated = reg["benefit"] > 0
    return {
        "schema": SCHEMA,
        "question": "How did an independently operated agent benefit from "
                    "AG's preflight evidence?",
        "independently_demonstrated_benefit": demonstrated,
        "headline": (
            f"{reg['benefit']} registered-participant benefit decision(s), "
            f"{reg['mistake']} mistake decision(s)"
            if demonstrated else
            "No independently demonstrated benefit yet."),
        "by_ownership": buckets,
        "unjoined_outcome_reports": unjoined,
        "reading_rules": {
            "registered_participant": "the SAME registered, non-first-party "
                                      "agent presented its key on the preflight "
                                      "AND the report; the only bucket that can "
                                      "satisfy the objective",
            "heuristic_external": "framework-looking User-Agents on both legs; "
                                  "software identity, NOT operator "
                                  "independence — never a headline",
            "propagation_client": "an AG-distributed client (e.g. the Pi "
                                  "extension) without a participant key",
            "first_party": "AG's own agents, tests and operator — evidence of "
                           "capability, never of adoption",
            "decisions_vs_report_events": "one decision per preflight_id; "
                                          "repeated identical reports are "
                                          "ignored; a later different report "
                                          "is a correction and the latest is "
                                          "classified",
            "claims": "reporter_kind and decision_basis are the reporter's "
                      "claims, labelled as such; nothing verifies them",
            "benefit": "requires a warning verdict, a reporter-run direct "
                       "check confirming the problem, AND a claimed decision "
                       "basis of ag_evidence/both; AG being right while the "
                       "agent decided on its own check is not a benefit",
            "scope": "a working call does not contradict identity/payment "
                     "warnings; a failure AG never claimed to test is "
                     "failure_out_of_scope; consistent_outcome is not benefit",
            "runs_without_outcome": "UNKNOWN — neither refusal nor success",
            "payment": "irrelevant; the path is free and payment proves nothing",
        },
        "rules": list(RULES),
        "measurement_coverage": coverage,
    }
