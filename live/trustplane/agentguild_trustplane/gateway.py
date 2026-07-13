"""The Gateway facade — the single choke point every integration calls.

    gw = Gateway(policy=RiskPolicy.load("policy.json"), state_dir="~/.agentguild")
    gate = gw.gate(capability="fact-check", value_at_risk=50.0)
    if gate.allowed:
        result = <invoke the chosen worker however your framework does>
        gw.report(gate, outcome="accepted", deliverable=result)

Everything else in the package exists to make these two calls correct:
evidence is fetched signed, verified locally, cached for outages, evaluated
under the CALLER's policy, and every delegation ends in a signed outcome.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .cache import SignedDecisionCache
from .client import GuildClient, DEFAULT_BASE
from .contract import decision_fresh
from .engine import evaluate
from .outcomes import OutcomeRecorder
from .policy import RiskPolicy, PolicyResult


def value_tier(amount: float) -> str:
    if amount < 10:
        return "micro"
    if amount < 100:
        return "low"
    if amount < 1000:
        return "medium"
    return "high"


class GateDenied(RuntimeError):
    """Raised by enforcing integrations when policy denies the delegation."""

    def __init__(self, result: "GateResult") -> None:
        self.result = result
        super().__init__("delegation denied by caller policy: "
                         + "; ".join(result.policy.reasons))


@dataclass
class GateResult:
    gate_id: str
    capability: str
    tier: str
    allowed: bool
    policy: PolicyResult
    decision: Optional[dict[str, Any]]
    routing: Optional[dict[str, Any]]
    channel: str                     # live | cache | outage
    gate_latency_ms: float
    worker_id: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id, "capability": self.capability,
            "tier": self.tier, "allowed": self.allowed,
            "policy": self.policy.to_json(), "decision": self.decision,
            "routing": self.routing, "channel": self.channel,
            "gate_latency_ms": self.gate_latency_ms,
            "worker_id": self.worker_id,
        }


class Gateway:
    def __init__(self, policy: Optional[RiskPolicy] = None,
                 state_dir: str | Path = "~/.agentguild",
                 base_url: str = DEFAULT_BASE,
                 api_key: Optional[str] = None,
                 ttl_seconds: int = 3600) -> None:
        self.policy = policy or RiskPolicy()
        self.state_dir = Path(state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cache = SignedDecisionCache(
            self.state_dir / "cache",
            trusted_issuers=self.policy.trusted_issuers or None)
        self.client = GuildClient(base_url, cache=self.cache, api_key=api_key)
        self.outcomes = OutcomeRecorder(self.state_dir, self.client)
        self.ttl_seconds = ttl_seconds
        self.metrics = {"gates": 0, "allowed": 0, "denied": 0,
                        "monitor_flagged": 0, "outage_gates": 0,
                        "gate_latency_ms": []}

    # ------------------------------------------------------------------ gate
    def gate(self, capability: str, value_at_risk: float = 0.0,
             context: Optional[dict[str, Any]] = None) -> GateResult:
        """Vet a delegation BEFORE it happens. Fetch (or cache-serve) the
        signed AGD-1 decision for the capability, evaluate the caller policy
        at the value tier, and return an actionable gate."""
        t0 = time.perf_counter()
        tier = value_tier(float(value_at_risk))
        envelope, channel, age = self.client.signed_decision(
            capability, ttl_seconds=self.ttl_seconds)
        decision = routing = None
        if envelope is not None:
            decision = envelope.get("decision")
            routing = envelope.get("routing")
            if channel == "cache":
                still_valid, age2 = decision_fresh(envelope)
                age = age2 if age2 is not None else age
        if envelope is None:
            pol = evaluate(None, self.policy, tier, fail_state="outage")
            self.metrics["outage_gates"] += 1
        else:
            pol = evaluate(decision, self.policy, tier,
                           decision_age_seconds=age, fail_state=channel)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        gr = GateResult(
            gate_id="gate_" + uuid.uuid4().hex[:12], capability=capability,
            tier=tier, allowed=pol.allowed, policy=pol, decision=decision,
            routing=routing, channel=channel, gate_latency_ms=latency_ms,
            worker_id=(decision or {}).get("agent_id"),
            meta={"context": context or {}})
        self.metrics["gates"] += 1
        self.metrics["gate_latency_ms"].append(latency_ms)
        if pol.allowed:
            self.metrics["allowed"] += 1
            if pol.reasons != ["all policy checks passed"] and \
               self.policy.mode == "monitor":
                self.metrics["monitor_flagged"] += 1
        else:
            self.metrics["denied"] += 1
        return gr

    # ---------------------------------------------------------------- report
    def report(self, gate: GateResult, outcome: str,
               deliverable: Optional[str] = None,
               latency_ms: Optional[float] = None,
               cost: Optional[float] = None,
               flush: bool = True) -> dict[str, Any]:
        """Record the delegation outcome (signed, queued, flushed). Call this
        for EVERY gated delegation — including denials (outcome='blocked')."""
        rec = self.outcomes.record(
            gate_id=gate.gate_id, capability=gate.capability,
            worker_id=gate.worker_id, outcome=outcome,
            deliverable=deliverable, latency_ms=latency_ms, cost=cost,
            policy_result=gate.policy.to_json())
        if flush:
            self.outcomes.flush()
        return rec

    # --------------------------------------------------------------- metrics
    def snapshot(self) -> dict[str, Any]:
        lat = sorted(self.metrics["gate_latency_ms"])
        pct = (lambda q: lat[min(len(lat) - 1, int(q * len(lat)))] if lat else None)
        return {
            "gates": self.metrics["gates"], "allowed": self.metrics["allowed"],
            "denied": self.metrics["denied"],
            "monitor_flagged": self.metrics["monitor_flagged"],
            "outage_gates": self.metrics["outage_gates"],
            "gate_latency_ms": {"p50": pct(0.5), "p95": pct(0.95)},
            "cache": self.cache.metrics(),
            "client": self.client.stats,
            "outcomes": self.outcomes.stats,
            "policy_id": self.policy.policy_id,
            "policy_mode": self.policy.mode,
        }
