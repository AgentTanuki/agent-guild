"""Policy evaluation: AGD-1 evidence × caller policy → PolicyResult.

Pure function, no I/O — this is the piece a caller can unit-test against
their own policy file, and the piece the conformance suite pins.
"""
from __future__ import annotations

from typing import Any, Optional

from .contract import validate_decision, staleness_days
from .policy import RiskPolicy, PolicyResult, PROVENANCE_ORDER


def _prov_rank(cls: Optional[str]) -> int:
    try:
        return PROVENANCE_ORDER.index(cls)  # type: ignore[arg-type]
    except ValueError:
        return -1


def evaluate(decision: Optional[dict[str, Any]], policy: RiskPolicy,
             tier: str, *, decision_age_seconds: Optional[float] = None,
             fail_state: str = "live") -> PolicyResult:
    """Evaluate one candidate decision under the caller's policy.

    ``decision`` is the AGD-1 object (or None when the Guild knows nothing).
    ``decision_age_seconds`` is the signed-envelope age when served from cache.
    ``fail_state`` labels the evidence channel: live | cache | outage.
    """
    rule = policy.rule(tier)
    reasons: list[str] = []

    if fail_state == "unverified":
        # The Guild ANSWERED but the document failed verification (tampered,
        # unknown issuer, expired, non-conformant, or counterparty-binding
        # violation) and no acceptable cache exists. This is an integrity
        # signal, not an availability signal: enforce mode ALWAYS denies —
        # the tier's outage fail-open never applies to bad evidence.
        return PolicyResult(
            allowed=policy.mode == "monitor",
            enforced=policy.mode == "enforce",
            policy_id=policy.policy_id, tier=tier,
            fail_state="unverified",
            reasons=["live document failed verification and no verifiable "
                     "cached decision exists — failing closed"],
            decision_age_seconds=None)

    if fail_state == "outage":
        # no live Guild AND no acceptable cache: the tier's fail mode decides.
        allowed = rule.fail_mode in ("open", "monitor")
        return PolicyResult(
            allowed=allowed if policy.mode == "enforce" else True,
            enforced=policy.mode == "enforce" and not allowed,
            policy_id=policy.policy_id, tier=tier,
            fail_state="outage_open" if allowed else "outage_closed",
            reasons=[f"guild unreachable and no fresh cached decision; "
                     f"tier '{tier}' fail_mode={rule.fail_mode}"],
            decision_age_seconds=None)

    if decision is None:
        reasons.append("no Guild evidence for candidate")
        allowed = not policy.deny_unknown_agents
    else:
        errs = validate_decision(decision)
        if errs:
            reasons.append("decision not AGD-1 conformant: " + "; ".join(errs[:4]))
        if decision_age_seconds is not None and \
           decision_age_seconds > rule.max_decision_age_seconds:
            reasons.append(
                f"cached decision too old: {decision_age_seconds:.0f}s > "
                f"{rule.max_decision_age_seconds:.0f}s")
        est = float(decision.get("estimate") or 0.0)
        conf = float(decision.get("confidence") or 0.0)
        if est < rule.min_estimate:
            reasons.append(f"estimate {est} < {rule.min_estimate}")
        if conf < rule.min_confidence:
            reasons.append(f"confidence {conf} < {rule.min_confidence}")
        if rule.max_staleness_days is not None:
            days = staleness_days(decision)
            if days is None:
                reasons.append("no dated evidence (staleness unknown)")
            elif days > rule.max_staleness_days:
                reasons.append(f"evidence stale: {days}d > {rule.max_staleness_days}d")
        if rule.require_reachable and not (
                decision.get("recommended_for_routing")
                or decision.get("reachability_status") in
                ("recently_reachable", "invocation_verified")):
            reasons.append("no VERIFIED reachable endpoint "
                           f"(status={decision.get('reachability_status')})")
        ident = decision.get("identity") or {}
        if rule.require_did_control_proven and not ident.get("did_control_proven"):
            reasons.append("DID control not cryptographically proven")
        if rule.min_provenance is not None:
            strongest = (decision.get("evidence_provenance") or {}).get("strongest")
            if _prov_rank(strongest) < _prov_rank(rule.min_provenance):
                reasons.append(f"strongest provenance {strongest!r} below "
                               f"required {rule.min_provenance!r}")
        if rule.require_tier_supported:
            tiers = ((decision.get("value_at_risk") or {}).get("tiers") or {})
            if not tiers.get(tier, False):
                reasons.append(f"evidence depth does not support tier '{tier}'")
        allowed = not reasons

    enforced = policy.mode == "enforce" and not allowed
    return PolicyResult(
        allowed=allowed or policy.mode == "monitor",
        enforced=enforced,
        policy_id=policy.policy_id, tier=tier, fail_state=fail_state,
        reasons=reasons or ["all policy checks passed"],
        decision_age_seconds=decision_age_seconds)
