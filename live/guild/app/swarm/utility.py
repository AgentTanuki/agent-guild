"""Machine-native utility model: estimate whether invoking an AG capability has
positive expected utility for an external machine, using machine-relevant
variables only (never clicks, prose attractiveness, or social engagement).

Used by GET /swarm/match to rank capabilities for a task description, and by
the wording-optimizer to correlate signals with actual invocation outcomes."""
from __future__ import annotations

import re
from typing import Optional

from .capabilities import CAPABILITIES, Capability
from .identity import registry

_WORD_RE = re.compile(r"[a-z0-9_.]+")

WEIGHTS = {
    "semantic_fit": 0.30,
    "historical_accuracy": 0.20,
    "success_probability": 0.15,
    "latency": 0.10,
    "cost": 0.05,
    "availability": 0.05,
    "trust_attestation": 0.05,
    "context_fit": 0.03,
    "privacy_fit": 0.03,
    "dependency_complexity": 0.02,
    "failure_recovery": 0.01,
    "composability": 0.01,
}


def _tokens(text: str) -> set:
    return set(_WORD_RE.findall(text.lower()))


def _semantic_fit(task: str, cap: Capability) -> float:
    task_t = _tokens(task)
    if not task_t:
        return 0.0
    cap_t = (_tokens(cap.summary) | _tokens(cap.description)
             | set(cap.tags) | _tokens(cap.id.replace(".", " ")))
    overlap = len(task_t & cap_t)
    return min(1.0, overlap / max(3, min(len(task_t), 10)))


def score(cap: Capability, task: str, counters: dict,
          sample_payload: Optional[dict] = None) -> dict:
    doc = registry.for_capability(cap.id)
    c = counters.get(cap.id, {})
    # reliability counts only capability-side outcomes: successes vs internal
    # failures. Caller payload errors (schema_validation/unprocessable) are the
    # caller's, and a correct structured rejection is not a failure.
    attempts = c.get("successes", 0) + c.get("failures", 0)
    live_success = (c.get("successes", 0) / attempts) if attempts else None
    bench = (doc["identity"]["benchmark"] if doc else {"ok": False})
    fixture_rate = ((bench.get("passed", 0) / bench.get("total", 1))
                    if bench.get("total") else 0.0)

    schema_compatible = None
    if sample_payload is not None:
        import jsonschema
        try:
            jsonschema.validate(sample_payload, cap.input_schema)
            schema_compatible = True
        except jsonschema.ValidationError:
            schema_compatible = False

    factors = {
        "semantic_fit": _semantic_fit(task, cap),
        "historical_accuracy": fixture_rate if live_success is None
        else 0.5 * fixture_rate + 0.5 * live_success,
        "success_probability": live_success if live_success is not None
        else fixture_rate,
        "latency": 1.0 if cap.est_latency_ms <= 50 else 0.8,
        "cost": 1.0,                                   # guest tier is free
        "availability": 1.0 if doc and doc["identity"]["health"] == "passing" else 0.0,
        "trust_attestation": 1.0,                      # Guild-signed identity + envelope
        "context_fit": 1.0,                            # 64KB payload cap, stateless
        "privacy_fit": 1.0,                            # no payload retention
        "dependency_complexity": 1.0,                  # zero caller-side deps (HTTP only)
        "failure_recovery": 1.0,                       # structured errors, idempotent
        "composability": 1.0 if cap.est_cost_credits == 0 else 0.8,
    }
    utility = sum(WEIGHTS[k] * v for k, v in factors.items())
    out = {"capability": cap.id, "utility": round(utility, 4),
           "factors": {k: round(v, 4) for k, v in factors.items()},
           "expected_latency_ms": cap.est_latency_ms,
           "guest_cost_credits": cap.est_cost_credits}
    if schema_compatible is not None:
        out["sample_payload_schema_compatible"] = schema_compatible
    if doc:
        out["ag_id"] = doc["identity"]["ag_id"]
        out["invoke"] = doc["identity"]["protocols"]["rest"]["url"]
    return out


def match(store, task: str, sample_payload: Optional[dict] = None,
          limit: int = 5) -> dict:
    counters = store.swarm_state.get("counters", {})
    scored = [score(cap, task, counters, sample_payload)
              for cap in CAPABILITIES.values()]
    scored.sort(key=lambda s: s["utility"], reverse=True)
    return {"task": task[:500], "weights": WEIGHTS, "matches": scored[:limit],
            "note": "utility estimates machine-relevant expected value; "
                    "semantic_fit is lexical in Pilot A"}
