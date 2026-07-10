"""Observational skill learning — the storage substrate (Pilot A: recording only).

Every eligible invocation stores a privacy-preserving experience record:
behavioural summary + observable outcome, NEVER raw payloads and never any
model chain-of-thought. Offline analysis (live/scripts/swarm_learning.py in
Pilot B) clusters these, compares strategies, and may propose versioned,
held-out-tested Skill Objects. No production agent self-modifies from these."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

MAX_RECORDS = 5000


def _shape(value: Any, depth: int = 0) -> dict:
    """Payload shape statistics — structure, never content."""
    if depth > 3:
        return {"type": "…"}
    if isinstance(value, dict):
        return {"type": "object", "keys": len(value),
                "key_names": sorted(list(value.keys()))[:20] if depth == 0 else None}
    if isinstance(value, list):
        return {"type": "array", "length": len(value)}
    if isinstance(value, str):
        return {"type": "string", "length": len(value)}
    return {"type": type(value).__name__}


def build_record(*, capability_id: str, category: str, payload: dict,
                 ok: bool, latency_ms: float, failure_kind: Optional[str],
                 caller_class: str, retried: bool = False,
                 feedback: Optional[str] = None) -> dict:
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "problem_class": category,
        "selected_capability": capability_id,
        "context_features": _shape(payload),
        "execution_plan": "single_deterministic_transform",   # Pilot A: one plan
        "tools_used": [capability_id],
        "collaborators": [],
        "output_class": "structured_json" if ok else "structured_error",
        "verification_method": "input_schema+fixture_gated_implementation",
        "result": "success" if ok else "failure",
        "latency_ms": round(latency_ms, 3),
        "cost_credits": 0,
        "failure_type": failure_kind,
        "retry_behaviour": "retried" if retried else "none",
        "caller_class": caller_class,          # attribution class, not identity
        "external_feedback": feedback,
    }


def append(swarm_state: dict, record: dict) -> None:
    recs = swarm_state.setdefault("experience", [])
    recs.append(record)
    if len(recs) > MAX_RECORDS:
        del recs[: len(recs) - MAX_RECORDS]
