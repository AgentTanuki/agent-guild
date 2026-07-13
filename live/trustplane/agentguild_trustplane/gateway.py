"""The Gateway facade — the single choke point every integration calls.

    gw = Gateway(policy=RiskPolicy.load("policy.json"), state_dir="~/.agentguild")
    gate = gw.gate(capability="fact-check", value_at_risk=50.0)
    if gate.allowed:
        result = <invoke gate.routing["endpoint"] — and ONLY that endpoint>
        gw.report(gate, outcome="accepted", deliverable=result)

Corrective pass 2026-07-13 — binding invariants enforced here:

  * the gateway FAILS CLOSED when a signed envelope's decision and routing
    concern different counterparties (one-counterparty invariant);
  * every GateResult is BOUND to the signed-envelope hash, provider id,
    provider DID, endpoint fingerprint, capability, policy id and value tier;
  * outcomes reference that binding and can never be credited to another
    provider (see outcomes.py / bind_destination);
  * an unverifiable live document is never evidence (channel "unverified").
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# the gate in force for the CURRENT delegation. A ContextVar (not thread-local)
# so it survives framework executors that hop the tool body onto a worker
# thread while COPYING the context (e.g. langgraph ToolNode's async path via
# langchain_core.run_in_executor).
_CURRENT_GATE: "contextvars.ContextVar[Optional[GateResult]]" = \
    contextvars.ContextVar("agentguild_current_gate", default=None)

from .cache import SignedDecisionCache
from .client import GuildClient, DEFAULT_BASE
from .contract import decision_fresh, binding_violations
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


def _sha256_json(doc: Any) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True,
                                     separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def endpoint_fingerprint(endpoint: Optional[str]) -> Optional[str]:
    if not endpoint:
        return None
    return "sha256:" + hashlib.sha256(str(endpoint).encode()).hexdigest()


class GateDenied(RuntimeError):
    """Raised by enforcing integrations when policy denies the delegation."""

    def __init__(self, result: "GateResult") -> None:
        self.result = result
        super().__init__("delegation denied by caller policy: "
                         + "; ".join(result.policy.reasons))


class DestinationMismatch(GateDenied):
    """An integration attempted to invoke an identity or endpoint OTHER than
    the one the signed decision evaluated. Always fails closed."""

    def __init__(self, result: "GateResult", detail: str) -> None:
        self.detail = detail
        RuntimeError.__init__(self, "destination does not match the signed "
                                    f"route (failing closed): {detail}")
        self.result = result


@dataclass
class GateResult:
    gate_id: str
    capability: str
    tier: str
    allowed: bool
    policy: PolicyResult
    decision: Optional[dict[str, Any]]
    routing: Optional[dict[str, Any]]
    channel: str                     # live | cache | unverified | outage
    gate_latency_ms: float
    worker_id: Optional[str] = None
    # --- binding (corrective 2026-07-13): what this gate is ABOUT -----------
    envelope_sha256: Optional[str] = None
    provider_did: Optional[str] = None
    endpoint: Optional[str] = None
    endpoint_sha256: Optional[str] = None
    policy_id: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)

    def binding(self) -> dict[str, Any]:
        """The immutable identity/destination binding outcomes must cite."""
        return {
            "gate_id": self.gate_id,
            "envelope_sha256": self.envelope_sha256,
            "provider_id": self.worker_id,
            "provider_did": self.provider_did,
            "endpoint": self.endpoint,
            "endpoint_sha256": self.endpoint_sha256,
            "capability": self.capability,
            "policy_id": self.policy_id,
            "tier": self.tier,
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id, "capability": self.capability,
            "tier": self.tier, "allowed": self.allowed,
            "policy": self.policy.to_json(), "decision": self.decision,
            "routing": self.routing, "channel": self.channel,
            "gate_latency_ms": self.gate_latency_ms,
            "worker_id": self.worker_id,
            "binding": self.binding(),
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
                        "binding_failures": 0, "gate_latency_ms": []}

    # ------------------------------------------------------------------ gate
    def gate(self, capability: str, value_at_risk: float = 0.0,
             context: Optional[dict[str, Any]] = None) -> GateResult:
        """Vet a delegation BEFORE it happens. Fetch (or cache-serve) the
        signed AGD-1 decision for the capability, verify it, enforce the
        one-counterparty binding, evaluate the caller policy at the value
        tier, and return an actionable, BOUND gate."""
        t0 = time.perf_counter()
        tier = value_tier(float(value_at_risk))
        envelope, channel, age = self.client.signed_decision(
            capability, ttl_seconds=self.ttl_seconds)
        decision = routing = None
        binding_errs: list[str] = []
        if envelope is not None:
            decision = envelope.get("decision")
            routing = envelope.get("routing")
            if channel == "cache":
                still_valid, age2 = decision_fresh(envelope)
                age = age2 if age2 is not None else age
            # FAIL CLOSED on any counterparty mismatch, whatever the channel.
            binding_errs = binding_violations(envelope)
            if binding_errs:
                self.metrics["binding_failures"] += 1
                decision = None
                routing = {"routable": False, "provider_id": None,
                           "reason": "counterparty binding violated: "
                                     + "; ".join(binding_errs[:3])}
        if envelope is None or binding_errs:
            fail_state = ("unverified"
                          if channel == "unverified" or binding_errs
                          else "outage")
            pol = evaluate(None, self.policy, tier, fail_state=fail_state)
            if binding_errs:
                pol.reasons.append("counterparty binding violated: "
                                   + "; ".join(binding_errs[:3]))
            self.metrics["outage_gates"] += 1
        else:
            pol = evaluate(decision, self.policy, tier,
                           decision_age_seconds=age, fail_state=channel)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        routable = bool((routing or {}).get("routable"))
        gr = GateResult(
            gate_id="gate_" + uuid.uuid4().hex[:12], capability=capability,
            tier=tier, allowed=pol.allowed, policy=pol, decision=decision,
            routing=routing, channel=channel, gate_latency_ms=latency_ms,
            worker_id=((routing or {}).get("provider_id") if routable
                       else (decision or {}).get("agent_id")),
            envelope_sha256=(_sha256_json(envelope)
                             if envelope is not None and not binding_errs
                             else None),
            provider_did=((routing or {}).get("provider_did") if routable
                          else ((decision or {}).get("identity") or {}
                                ).get("did")),
            endpoint=(routing or {}).get("endpoint") if routable else None,
            endpoint_sha256=((routing or {}).get("endpoint_sha256")
                             if routable else None),
            policy_id=self.policy.policy_id,
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
        _CURRENT_GATE.set(gr)
        return gr

    # -------------------------------------------------------- current gate
    def current_gate(self) -> Optional["GateResult"]:
        """The gate in force for the CURRENT delegation context — how a tool
        body invoked through an interceptor learns its signed route without
        the harness hand-calling gate() inside the tool. Context-scoped so it
        survives framework executors that copy the context onto a worker
        thread."""
        return _CURRENT_GATE.get()

    # ---------------------------------------------------- destination binding
    def bind_destination(self, gate: GateResult, *,
                         endpoint: Optional[str] = None,
                         provider_id: Optional[str] = None,
                         provider_did: Optional[str] = None) -> None:
        """Assert an explicitly-supplied destination EXACTLY matches the
        signed route this gate was issued for. Raises DestinationMismatch
        (fail closed) on ANY divergence — endpoint substitution and identity
        substitution are rejected here for every adapter."""
        if endpoint is not None:
            if gate.endpoint is None:
                raise DestinationMismatch(
                    gate, "an explicit endpoint was supplied but the signed "
                          "route has none (not routable)")
            if endpoint != gate.endpoint or \
               endpoint_fingerprint(endpoint) != gate.endpoint_sha256:
                raise DestinationMismatch(
                    gate, f"endpoint {endpoint!r} != routed {gate.endpoint!r}")
        if provider_id is not None and provider_id != gate.worker_id:
            raise DestinationMismatch(
                gate, f"provider_id {provider_id!r} != evaluated "
                      f"{gate.worker_id!r}")
        if provider_did is not None and provider_did != gate.provider_did:
            raise DestinationMismatch(
                gate, f"provider_did {provider_did!r} != evaluated "
                      f"{gate.provider_did!r}")
        gate.meta["destination_bound"] = True

    # ---------------------------------------------------------------- report
    def report(self, gate: GateResult, outcome: str,
               deliverable: Optional[str] = None,
               latency_ms: Optional[float] = None,
               cost: Optional[float] = None,
               flush: bool = True) -> dict[str, Any]:
        """Record the delegation outcome (signed, bound, queued, flushed).
        Call this for EVERY gated delegation — including denials
        (outcome='blocked'). The outcome cites the gate's binding: it can
        never be credited to a provider the gate did not evaluate."""
        rec = self.outcomes.record(
            binding=gate.binding(), outcome=outcome,
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
            "binding_failures": self.metrics["binding_failures"],
            "gate_latency_ms": {"p50": pct(0.5), "p95": pct(0.95)},
            "cache": self.cache.metrics(),
            "client": self.client.stats,
            "outcomes": self.outcomes.stats,
            "policy_id": self.policy.policy_id,
            "policy_mode": self.policy.mode,
        }
