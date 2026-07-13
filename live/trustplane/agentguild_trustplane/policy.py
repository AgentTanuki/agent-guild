"""Caller-owned risk policies.

A policy maps VALUE-AT-RISK TIERS (micro/low/medium/high — market.value_tier
semantics) to threshold rules over AGD-1 evidence, plus an explicit fail mode
for when no fresh evidence is reachable (Guild outage AND cache stale):

  fail_mode="open"    delegate anyway, flagged (cheap, reversible work)
  fail_mode="closed"  refuse to delegate (expensive/irreversible work)

The Guild never supplies these numbers. Defaults below are documented
starting points a caller is expected to edit — that is the point.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

TIERS = ("micro", "low", "medium", "high")
# provenance classes ordered weakest -> strongest (mirrors ledger weights)
PROVENANCE_ORDER = ("one_party_claim", "first_party_bootstrap",
                    "external_import", "mutual_attestation",
                    "verifiable_outcome", "guild_mediated")


@dataclass
class TierRule:
    min_estimate: float = 0.0
    min_confidence: float = 0.0
    max_staleness_days: Optional[float] = None     # None = no bound
    require_reachable: bool = False                # verified-reachable endpoint
    require_did_control_proven: bool = False
    min_provenance: Optional[str] = None           # weakest acceptable strongest-class
    require_tier_supported: bool = True            # decision.value_at_risk.tiers[tier]
    max_decision_age_seconds: float = 3600.0       # signed-envelope freshness
    fail_mode: str = "closed"                      # open | closed | monitor

    def __post_init__(self) -> None:
        if self.fail_mode not in ("open", "closed", "monitor"):
            raise ValueError("fail_mode must be open|closed|monitor")
        if self.min_provenance is not None and \
           self.min_provenance not in PROVENANCE_ORDER:
            raise ValueError(f"unknown provenance class {self.min_provenance}")


@dataclass
class RiskPolicy:
    """A caller's complete delegation policy."""
    policy_id: str = "default"
    # mode: "enforce" blocks denied delegations; "monitor" lets them through
    # but records the counterfactual — the adoption on-ramp.
    mode: str = "enforce"
    tiers: dict[str, TierRule] = field(default_factory=dict)
    # issuer trust: which decision issuers (DIDs) this caller accepts.
    trusted_issuers: list[str] = field(default_factory=list)  # empty = pin at first use
    deny_unknown_agents: bool = True   # no decision/no evidence => deny (enforce)

    def __post_init__(self) -> None:
        if self.mode not in ("enforce", "monitor"):
            raise ValueError("mode must be enforce|monitor")
        base = default_tiers()
        for t in TIERS:
            self.tiers.setdefault(t, base[t])

    def rule(self, tier: str) -> TierRule:
        return self.tiers[tier if tier in self.tiers else "high"]

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "RiskPolicy":
        tiers = {k: TierRule(**v) for k, v in (d.get("tiers") or {}).items()}
        return cls(policy_id=d.get("policy_id", "default"),
                   mode=d.get("mode", "enforce"), tiers=tiers,
                   trusted_issuers=list(d.get("trusted_issuers") or []),
                   deny_unknown_agents=bool(d.get("deny_unknown_agents", True)))

    @classmethod
    def load(cls, path: str | Path) -> "RiskPolicy":
        return cls.from_json(json.loads(Path(path).read_text()))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=2))


def default_tiers() -> dict[str, TierRule]:
    return {
        "micro": TierRule(min_estimate=0.05, min_confidence=0.0,
                          max_staleness_days=None, require_reachable=False,
                          min_provenance=None, require_tier_supported=False,
                          max_decision_age_seconds=6 * 3600, fail_mode="open"),
        "low": TierRule(min_estimate=0.15, min_confidence=0.1,
                        max_staleness_days=90, require_reachable=False,
                        min_provenance="mutual_attestation",
                        max_decision_age_seconds=3 * 3600, fail_mode="open"),
        "medium": TierRule(min_estimate=0.3, min_confidence=0.3,
                           max_staleness_days=30, require_reachable=True,
                           min_provenance="verifiable_outcome",
                           max_decision_age_seconds=3600, fail_mode="closed"),
        "high": TierRule(min_estimate=0.5, min_confidence=0.5,
                         max_staleness_days=30, require_reachable=True,
                         require_did_control_proven=True,
                         min_provenance="guild_mediated",
                         max_decision_age_seconds=900, fail_mode="closed"),
    }


@dataclass
class PolicyResult:
    """The caller-computed policy leg of AGD-1 — what fills decision.policy."""
    allowed: bool
    enforced: bool                  # False in monitor mode (would-have-denied)
    policy_id: str
    tier: str
    fail_state: str                 # "live" | "cache" | "outage_open" | "outage_closed"
    reasons: list[str]
    decision_age_seconds: Optional[float] = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)
