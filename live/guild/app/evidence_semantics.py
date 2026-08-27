"""Precise, machine-readable semantics for task outcomes and evidence.

The Guild records assertions, handoffs, observations, and settlements.  Those
facts are useful, but they are not interchangeable.  This leaf module keeps the
labels honest everywhere they are served and makes neutral task outcomes
explicit so persistence pressure cannot turn an honest stop into a reputation
failure.
"""
from __future__ import annotations

from typing import Any, Optional


POSITIVE_OUTCOMES = frozenset({"accepted"})
NEGATIVE_OUTCOMES = frozenset({"rejected", "disputed"})
NEUTRAL_OUTCOMES = frozenset({
    "delivered",       # work exists, but the requester has not graded it
    "declined",        # worker did not accept/continue the task
    "infeasible",      # task cannot be completed as specified
    "blocked",         # an external dependency or authority boundary stopped it
    "cannot_verify",   # claimed result cannot be verified honestly
})
HONEST_STOP_OUTCOMES = NEUTRAL_OUTCOMES - {"delivered"}
PENDING_OUTCOMES = frozenset({"open"})
RECEIPT_OUTCOMES = POSITIVE_OUTCOMES | NEGATIVE_OUTCOMES | NEUTRAL_OUTCOMES
SIGNED_OUTCOMES = POSITIVE_OUTCOMES | NEGATIVE_OUTCOMES | (
    NEUTRAL_OUTCOMES - {"delivered"}
)
# Only terminal task states are backfilled as collaboration records. A
# `delivered` receipt remains an append-only raw event until a requester grades
# it, otherwise startup would seal the transitional state and make grading
# impossible.
LEDGER_TASK_OUTCOMES = POSITIVE_OUTCOMES | NEGATIVE_OUTCOMES | HONEST_STOP_OUTCOMES
GRADE_CLAIMANT_ROLES = frozenset({"requester", "independent_verifier"})


def outcome_effect(outcome: str) -> str:
    if outcome in POSITIVE_OUTCOMES:
        return "positive"
    if outcome in NEGATIVE_OUTCOMES:
        return "negative"
    if outcome in NEUTRAL_OUTCOMES:
        return "neutral"
    if outcome in PENDING_OUTCOMES:
        return "pending"
    raise ValueError(f"unknown task outcome: {outcome!r}")


def outcome_semantics(outcome: str, *, claimant_role: Optional[str] = None,
                      phase: Optional[str] = None,
                      reason_code: Optional[str] = None) -> dict[str, Any]:
    effect = outcome_effect(outcome)
    attributable = not (
        effect in ("positive", "negative")
        and claimant_role is not None
        and claimant_role not in GRADE_CLAIMANT_ROLES
    )
    reputation_effect = effect if attributable else "none"
    return {
        "outcome": outcome,
        "reported_effect": effect,
        "reputation_effect": reputation_effect,
        "scoreable": attributable and effect in ("positive", "negative"),
        "success": (True if reputation_effect == "positive"
                    else False if reputation_effect == "negative" else None),
        "claimant_role": claimant_role,
        "phase": phase,
        "reason_code": reason_code,
        "note": (
            "Neutral outcomes are retained as history but excluded from success-rate "
            "and reputation calculations."
            if effect == "neutral" else
            "Positive and negative outcomes contribute according to provenance weight."
            if reputation_effect in ("positive", "negative") else
            "The reported grade is retained but is not scoring evidence because "
            "it was not attributed to the requester or an independent verifier."
            if effect in ("positive", "negative") else
            "No outcome has been reported yet."
        ),
    }


def task_outcome_is_scoreable(task: dict[str, Any]) -> bool:
    """Whether a served task carries an attributable terminal grade.

    Historical tasks without claimant metadata retain their prior treatment;
    every new receipt records a claimant role and therefore fails closed.
    """
    outcome = str(task.get("outcome") or "open")
    if outcome not in (POSITIVE_OUTCOMES | NEGATIVE_OUTCOMES):
        return False
    claimant = (task.get("metadata") or {}).get("outcome_claimant_role")
    return claimant is None or claimant in GRADE_CLAIMANT_ROLES


_LABELS: dict[str, dict[str, Any]] = {
    "no_receipt": {
        "proves": [],
        "does_not_prove": ["delivery", "execution", "outcome", "quality"],
    },
    "participant_claim": {
        "proves": [
            "the authenticated or signed participant made the recorded claim",
            "the record is bound to the referenced task and content hash when present",
        ],
        "does_not_prove": [
            "that execution occurred as claimed",
            "that the outcome is correct, safe, or high quality",
            "independent agreement with the claim",
        ],
    },
    "unattributed_claim": {
        "proves": [
            "the Guild recorded the claim and its task/content binding when present",
        ],
        "does_not_prove": [
            "who made or authorised the claim",
            "that execution occurred as claimed",
            "that the outcome is correct, safe, or high quality",
        ],
    },
    "bilateral_handoff": {
        "proves": [
            "both named task parties cryptographically participated",
            "the worker receipt and requester attestation refer to the same task",
        ],
        "does_not_prove": [
            "independent execution correctness",
            "deliverable safety or quality beyond the parties' claims",
        ],
    },
    "guild_observed_invocation": {
        "proves": [
            "the Guild initiated the bound invocation",
            "the Guild observed a protocol response linked to the task",
        ],
        "does_not_prove": [
            "that the response is factually correct, safe, or high quality",
            "the worker's hidden execution path or causal reasoning",
        ],
    },
    "independent_settlement": {
        "proves": [
            "the configured settlement completed through the Guild",
            "the settlement is bound to the task",
        ],
        "does_not_prove": [
            "independent verification of execution or work quality",
            "that payment implies a correct or safe outcome",
        ],
    },
    "independently_verified_outcome": {
        "proves": [
            "an identified independent verifier checked the outcome under a named method",
        ],
        "does_not_prove": [
            "claims outside the verifier's stated method and scope",
        ],
        "availability": "reserved; no current public write path assigns this label",
    },
}


def taxonomy() -> dict[str, dict[str, Any]]:
    """Return a copy so callers cannot mutate the canonical definitions."""
    return {name: {k: list(v) if isinstance(v, list) else v
                   for k, v in body.items()}
            for name, body in _LABELS.items()}


def _relation(label: str, *, source: str) -> dict[str, Any]:
    return {"label": label, "source": source, **taxonomy()[label]}


def _semantic(relations: list[dict[str, Any]], outcome: str, *,
              claimant_role: Optional[str] = None,
              phase: Optional[str] = None,
              reason_code: Optional[str] = None) -> dict[str, Any]:
    if not relations:
        relations = [_relation("no_receipt", source="task_state")]
    primary = relations[0]
    proves = list(dict.fromkeys(
        item for relation in relations for item in (relation.get("proves") or [])))
    does_not_prove = list(dict.fromkeys(
        item for relation in relations
        for item in (relation.get("does_not_prove") or [])))
    return {
        "contract": "AGOE-1/1.0",
        # A compound record cannot be truthfully collapsed to one strongest
        # label. Consumers inspect `relations`; these aggregate fields are only
        # a compact summary.
        "label": primary["label"] if len(relations) == 1 else "compound",
        "source": primary["source"] if len(relations) == 1 else "multiple",
        "proves": proves,
        "does_not_prove": does_not_prove,
        "relations": relations,
        "outcome": outcome_semantics(
            outcome, claimant_role=claimant_role, phase=phase,
            reason_code=reason_code),
    }


def for_task(task: dict[str, Any]) -> dict[str, Any]:
    """Interpret the currently served task/receipt without strengthening it."""
    meta = task.get("metadata") or {}
    outcome = str(task.get("outcome") or "open")
    if not task.get("deliverable_hash") and outcome == "open":
        return _semantic([], outcome)
    receipt_auth = str(meta.get("receipt_auth") or "unauthenticated")
    claim_label = ("unattributed_claim" if receipt_auth == "unauthenticated"
                   else "participant_claim")
    relations = [_relation(claim_label, source=receipt_auth)]
    worker_hash_matches = (
        ("worker_receipt_hash" not in meta and meta.get("worker_receipt_auth"))
        or (bool(meta.get("worker_receipt_hash"))
            and meta.get("worker_receipt_hash") == task.get("deliverable_hash"))
    )
    if worker_hash_matches and meta.get("grade_auth"):
        relations.append(_relation("bilateral_handoff", source="two_party_crypto"))
    if meta.get("guild_observed_invocation"):
        relations.append(_relation("guild_observed_invocation",
                                   source="guild_observed_invocation"))
    if isinstance(meta.get("settlement"), dict):
        relations.append(_relation("independent_settlement", source="settlement"))
    return _semantic(
        relations, outcome, claimant_role=meta.get("outcome_claimant_role"),
        phase=meta.get("outcome_phase"),
        reason_code=meta.get("outcome_reason_code"))


def for_record(record: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Interpret a sealed collaboration or generic signed-outcome record.

    This is deliberately a read-time interpretation, so historical sealed bytes
    remain untouched while old records receive the same honest disclosure.
    """
    record = record or {}
    record_type = record.get("type")
    if record_type == "signed_outcome":
        body = record.get("body") or {}
        return _semantic(
            [_relation("participant_claim", source="requester_signed_outcome")],
            str(body.get("outcome") or ""), claimant_role="requester",
            phase="reported_outcome")
    if record_type in ("receipt", "task_outcome"):
        body = record.get("body") or {}
        outcome = str(body.get("outcome") or "")
        receipt_auth = str(body.get("receipt_auth") or "unauthenticated")
        label = ("unattributed_claim" if receipt_auth == "unauthenticated"
                 else "participant_claim")
        relations = [_relation(label, source=receipt_auth)]
        worker_hash_matches = (
            ("worker_receipt_hash" not in body and body.get("worker_receipt_auth"))
            or (bool(body.get("worker_receipt_hash"))
                and body.get("worker_receipt_hash") == body.get("deliverable_hash"))
        )
        if worker_hash_matches and body.get("grade_auth"):
            relations.append(_relation("bilateral_handoff", source="two_party_crypto"))
        if body.get("guild_observed_invocation"):
            relations.append(_relation("guild_observed_invocation",
                                       source="guild_observed_invocation"))
        if isinstance(body.get("settlement"), dict):
            relations.append(_relation("independent_settlement", source="settlement"))
        return _semantic(
            relations, outcome,
            claimant_role=body.get("outcome_claimant_role"),
            phase=body.get("outcome_phase"),
            reason_code=body.get("outcome_reason_code"))
    if record_type:
        return {
            "contract": "AGOE-1/1.0",
            "label": "not_applicable",
            "source": str(record_type),
            "proves": [],
            "does_not_prove": ["a task outcome or work quality"],
            "relations": [],
            "outcome": None,
        }
    evidence = record.get("evidence") or {}
    embedded = evidence.get("semantics")
    if isinstance(embedded, dict) and embedded.get("contract") == "AGOE-1/1.0":
        return dict(embedded)
    basis = evidence.get("basis")
    receipt_auth = str(evidence.get("receipt_auth") or "")
    attributable = bool(receipt_auth and receipt_auth != "unauthenticated") or bool(
        record.get("signers"))
    relations = [_relation(
        "participant_claim" if attributable else "unattributed_claim",
        source=receipt_auth or str(record.get("provenance") or "record"))]
    if (basis == "two_party_crypto"
            or (evidence.get("attestation_ids")
                and evidence.get("receipt_auth") in ("worker_key", "worker_signature"))):
        relations.append(_relation("bilateral_handoff", source="two_party_crypto"))
    if evidence.get("invocation_id") or basis == "guild_observed_invocation":
        relations.append(_relation("guild_observed_invocation",
                                   source="guild_observed_invocation"))
    if evidence.get("settlement") or basis == "independent_settlement":
        relations.append(_relation("independent_settlement", source="settlement"))
    if basis == "independently_verified_outcome":
        relations.append(_relation("independently_verified_outcome",
                                   source="independent_outcome_verification"))
    return _semantic(
        relations, str(record.get("outcome") or ""),
        claimant_role=evidence.get("outcome_claimant_role"),
        phase=evidence.get("outcome_phase"),
        reason_code=evidence.get("outcome_reason_code"))


def for_record_evidence(evidence: dict[str, Any], outcome: str,
                        provenance: str) -> dict[str, Any]:
    """Build the interpretation embedded in newly sealed record bytes."""
    return for_record({"evidence": dict(evidence), "outcome": outcome,
                       "provenance": provenance})
