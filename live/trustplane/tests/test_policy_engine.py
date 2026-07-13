"""Pure policy-engine semantics: thresholds, fail modes, monitor mode."""
from __future__ import annotations

from agentguild_trustplane.engine import evaluate
from agentguild_trustplane.policy import RiskPolicy, TierRule


def _decision(**over):
    d = {
        "contract": "AGD-1/1.0", "agent_id": "agent_x",
        "identity": {"did": "did:key:zX", "did_method": "did:key",
                     "custodial": True, "did_control_proven": False,
                     "first_party": False},
        "capability_match": {"requested": "cap", "match": "exact",
                             "agent_capabilities": ["cap"]},
        "estimate": 0.4, "confidence": 0.5,
        "staleness": {"most_recent_at": "x", "age_days": 2.0, "label": "fresh"},
        "value_at_risk": {"tiers": {"micro": True, "low": True,
                                    "medium": False, "high": False},
                          "max_supported_tier": "low", "basis": "b"},
        "evidence_provenance": {"counts": {"mutual_attestation": 4},
                                "strongest": "mutual_attestation",
                                "verifiable_collaborations": 4,
                                "rules_version": "prov-v2",
                                "checkpoint": {"index": 0}},
        "policy": {"result": None, "decided_by": "caller"},
        "has_declared_endpoint": True,
        "reachability_status": "declared_unverified",
    }
    d.update(over)
    return d


def test_micro_allows_thin_evidence():
    r = evaluate(_decision(), RiskPolicy(), "micro")
    assert r.allowed and r.fail_state == "live"


def test_high_tier_denies_thin_evidence_with_reasons():
    r = evaluate(_decision(), RiskPolicy(), "high")
    assert not r.allowed
    text = " ".join(r.reasons)
    assert "provenance" in text and "DID control" in text


def test_caller_owns_thresholds():
    lax = RiskPolicy(policy_id="lax",
                     tiers={"high": TierRule(min_estimate=0.0, fail_mode="open",
                                             require_tier_supported=False)})
    assert evaluate(_decision(), lax, "high").allowed


def test_unknown_agent_denied_by_default_allowed_when_configured():
    assert not evaluate(None, RiskPolicy(), "low").allowed
    p = RiskPolicy(deny_unknown_agents=False)
    assert evaluate(None, p, "low").allowed


def test_outage_fail_modes():
    p = RiskPolicy()
    assert evaluate(None, p, "micro", fail_state="outage").allowed      # open
    assert not evaluate(None, p, "high", fail_state="outage").allowed   # closed
    r = evaluate(None, p, "high", fail_state="outage")
    assert r.fail_state == "outage_closed"


def test_monitor_mode_flags_but_does_not_block():
    p = RiskPolicy(mode="monitor")
    r = evaluate(_decision(estimate=0.0), p, "high")
    assert r.allowed and r.reasons != ["all policy checks passed"]


def test_stale_cached_decision_rejected_per_tier():
    p = RiskPolicy()
    r = evaluate(_decision(), p, "micro",
                 decision_age_seconds=7 * 3600, fail_state="cache")
    assert not r.allowed and any("too old" in x for x in r.reasons)
