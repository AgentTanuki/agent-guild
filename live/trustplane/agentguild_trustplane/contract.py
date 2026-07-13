"""AGD-1: the stable decision contract of the trust plane.

The contract is the JSON object served under ``decision`` by ``GET /check``
(and, signed, by ``GET /check?signed=true``). It contains EVIDENCE, never a
verdict: hire/caution/avoid is legacy presentation; thresholds belong to the
caller (see policy.py). Required legs:

  identity              did, did_method, custodial, did_control_proven, first_party
  capability_match      requested, match, agent_capabilities
  estimate              [0,1] trust estimate
  confidence            [0,1] evidence backing
  staleness             {most_recent_at, age_days, label} | null
  reachability          contact/has_declared_endpoint/reachability_status/...
  value_at_risk         {tiers, max_supported_tier, basis}
  evidence_provenance   {counts, strongest, verifiable_collaborations,
                         rules_version, checkpoint}
  policy                caller-owned slot ({result: null, decided_by: "caller"})

This module validates shape without any dependency on the Guild server, so
integrators can vendor it.
"""
from __future__ import annotations

from typing import Any, Optional

from .verify import within_validity

CONTRACT_ID = "AGD-1/1.0"

REQUIRED_FIELDS = (
    "contract", "agent_id", "identity", "capability_match", "estimate",
    "confidence", "staleness", "value_at_risk", "evidence_provenance",
    "policy",
)
REQUIRED_IDENTITY = ("did", "did_method", "custodial", "did_control_proven")
REQUIRED_VAR = ("tiers", "max_supported_tier", "basis")
REQUIRED_PROVENANCE = ("counts", "strongest", "verifiable_collaborations",
                       "rules_version", "checkpoint")
# reachability keys live at the top level of the decision (back-compat with
# schema v2 readers that consume decision.contact / reachability_status).
REQUIRED_REACHABILITY = ("has_declared_endpoint", "reachability_status")

VALUE_TIERS = ("micro", "low", "medium", "high")


def validate_decision(decision: dict[str, Any]) -> list[str]:
    """Return a list of contract violations (empty == conformant)."""
    errs: list[str] = []
    if not isinstance(decision, dict):
        return ["decision is not an object"]
    for f in REQUIRED_FIELDS:
        if f not in decision:
            errs.append(f"missing field: {f}")
    if decision.get("contract") != CONTRACT_ID:
        errs.append(f"contract != {CONTRACT_ID}")
    ident = decision.get("identity") or {}
    for f in REQUIRED_IDENTITY:
        if f not in ident:
            errs.append(f"identity missing: {f}")
    var = decision.get("value_at_risk") or {}
    for f in REQUIRED_VAR:
        if f not in var:
            errs.append(f"value_at_risk missing: {f}")
    for t in VALUE_TIERS:
        if t not in (var.get("tiers") or {}):
            errs.append(f"value_at_risk.tiers missing: {t}")
    prov = decision.get("evidence_provenance") or {}
    for f in REQUIRED_PROVENANCE:
        if f not in prov:
            errs.append(f"evidence_provenance missing: {f}")
    for f in REQUIRED_REACHABILITY:
        if f not in decision:
            errs.append(f"reachability missing: {f}")
    est, conf = decision.get("estimate"), decision.get("confidence")
    if not (isinstance(est, (int, float)) and 0.0 <= est <= 1.0):
        errs.append("estimate not in [0,1]")
    if not (isinstance(conf, (int, float)) and 0.0 <= conf <= 1.0):
        errs.append("confidence not in [0,1]")
    pol = decision.get("policy") or {}
    if pol.get("decided_by") != "caller":
        errs.append("policy.decided_by must be 'caller' as served")
    return errs


def decision_fresh(signed_envelope: dict[str, Any]) -> tuple[bool, Optional[float]]:
    """Freshness of a SIGNED decision envelope (type AgentGuildDecision)."""
    return within_validity(signed_envelope)


def binding_violations(envelope: dict[str, Any]) -> list[str]:
    """ONE-COUNTERPARTY INVARIANT (corrective pass 2026-07-13): when the
    envelope's routing gate says routable, the decision MUST be about that
    exact provider — same agent id, same DID, same endpoint and endpoint
    fingerprint, same requested capability. Returns violations (empty == ok);
    gateways FAIL CLOSED on any violation."""
    errs: list[str] = []
    routing = envelope.get("routing") or {}
    if not routing.get("routable"):
        return errs
    decision = envelope.get("decision")
    if not isinstance(decision, dict):
        return ["routable=true but no decision object"]
    if decision.get("agent_id") != routing.get("provider_id"):
        errs.append(f"decision.agent_id {decision.get('agent_id')!r} != "
                    f"routing.provider_id {routing.get('provider_id')!r}")
    ident = decision.get("identity") or {}
    if not routing.get("provider_did"):
        errs.append("routing.provider_did missing")
    elif ident.get("did") != routing.get("provider_did"):
        errs.append("decision.identity.did != routing.provider_did")
    if decision.get("endpoint") != routing.get("endpoint"):
        errs.append("decision.endpoint != routing.endpoint")
    if not routing.get("endpoint_sha256"):
        errs.append("routing.endpoint_sha256 missing")
    elif decision.get("endpoint_sha256") != routing.get("endpoint_sha256"):
        errs.append("decision.endpoint_sha256 != routing.endpoint_sha256")
    cap = (decision.get("capability_match") or {}).get("requested")
    if envelope.get("capability") is not None and \
       cap != envelope.get("capability"):
        errs.append("decision capability != envelope capability")
    return errs


def staleness_days(decision: dict[str, Any]) -> Optional[float]:
    s = decision.get("staleness")
    if isinstance(s, dict):
        return s.get("age_days")
    return None
