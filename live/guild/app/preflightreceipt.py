"""Signed, privacy-preserving receipts for free endpoint preflights.

The receipt is evidence that Agent Guild observed one exact run.  It is not
caller authentication and never grants delegation, payment, or other action
authority.  The high-entropy observation id stays in the caller's response;
public instrumentation receives only its SHA-256 commitment.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from . import coordination, crypto, preflight


CONTRACT = "AGPF-1/1.0"


def _private_commitment(observation_id: str, label: str, value: Any) -> str:
    """Domain-separated commitment salted by the unpublished observation id.

    A plain hash of a common endpoint is dictionary-reversible.  Salting with
    the receipt token keeps target/result guesses unlinkable until the holder
    deliberately reveals the signed receipt.
    """
    canonical = crypto.canonicalize_jcs(value)
    material = f"{CONTRACT}|{label}|{observation_id}|{canonical}".encode("utf-8")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _observation_commitment(observation_id: str) -> str:
    return "sha256:" + hashlib.sha256(
        observation_id.encode("utf-8")
    ).hexdigest()


def _result_without_receipt(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in result.items()
        if key != "receipt"
    }


def issue(store: Any, *, target: str, result: dict[str, Any],
          transport: str) -> dict[str, Any]:
    """Issue a durable offline-verifiable receipt without publishing its token."""
    gid = store.guild_identity()
    issued_at = datetime.now(timezone.utc).isoformat()
    observation_id = "pfobs_" + secrets.token_urlsafe(32)
    unsigned: dict[str, Any] = {
        "type": "AgentGuildPreflightReceipt",
        "contract": CONTRACT,
        "issuer": gid["did"],
        "issued_at": issued_at,
        "observation_id": observation_id,
        "observation_commitment": _observation_commitment(observation_id),
        "transport": transport,
        "target_commitment": _private_commitment(
            observation_id, "target", {"url": target}),
        "result_commitment": _private_commitment(
            observation_id, "result", _result_without_receipt(result)),
        "result_summary": {
            "verdict": result.get("verdict"),
            "failed_count": len(result.get("failed") or []),
            "unknown_count": len(result.get("unknowns") or []),
        },
        "signature_semantics": coordination.signature_semantics(),
    }
    proof: dict[str, Any] = {
        "type": "DataIntegrityProof",
        "cryptosuite": "eddsa-jcs-2022",
        "created": issued_at,
        "verificationMethod": crypto.did_key_verification_method(gid["did"]),
        "proofPurpose": "assertionMethod",
    }
    proof["proofValue"] = crypto.sign_eddsa_jcs(
        unsigned, proof, gid["private_key"]
    )
    return {**unsigned, "proof": proof}


def event_commitments(receipt: dict[str, Any]) -> dict[str, Any]:
    """Fields safe for the public activity feed; never include the bearer id."""
    return {
        "receipt_contract": receipt["contract"],
        "observation_commitment": receipt["observation_commitment"],
        "target_commitment": receipt["target_commitment"],
        "result_commitment": receipt["result_commitment"],
    }


def run_and_record(store: Any, url: str, *, actor: str | None, ua: str,
                   transport: str, actor_distinct: bool | None = None,
                   first_party: bool | None = None
                   ) -> dict[str, Any]:
    """Run, sign and durably record exactly one free preflight observation."""
    result = preflight.run(url, store=store)
    receipt = issue(store, target=url, result=result, transport=transport)
    event: dict[str, Any] = {
        "endpoint": "preflight",
        "transport": transport,
        "verdict": result.get("verdict"),
        "failed_count": len(result.get("failed") or []),
        "unknown_count": len(result.get("unknowns") or []),
        **event_commitments(receipt),
    }
    if actor_distinct is not None:
        event["actor_distinct"] = actor_distinct
    if first_party is not None:
        event["fp"] = first_party
    # The exact URL can contain query credentials.  The result receipt and the
    # durable event both bind it cryptographically, but instrumentation never
    # stores or republishes the raw value.
    store.record_event(actor, "preflight_run", ua=ua, **event)
    return {**result, "receipt": receipt}


def verify(store: Any, receipt: dict[str, Any], *, target: str | None = None,
           result: dict[str, Any] | None = None) -> dict[str, Any]:
    """Verify receipt origin/integrity and optional exact target/result inputs."""
    if not isinstance(receipt, dict):
        return {"valid": False, "error": "receipt_must_be_object"}
    proof = receipt.get("proof")
    if not isinstance(proof, dict):
        return {"valid": False, "error": "proof_missing"}
    unsigned = {key: value for key, value in receipt.items() if key != "proof"}
    issuer = str(unsigned.get("issuer") or "")
    observation_id = str(unsigned.get("observation_id") or "")
    trusted_issuer = store.guild_identity()["did"]
    issuer_trusted = issuer == trusted_issuer
    summary = unsigned.get("result_summary")
    summary_shape_valid = (
        isinstance(summary, dict)
        and set(summary) == {"verdict", "failed_count", "unknown_count"}
        and isinstance(summary.get("verdict"), str)
        and isinstance(summary.get("failed_count"), int)
        and not isinstance(summary.get("failed_count"), bool)
        and summary.get("failed_count", -1) >= 0
        and isinstance(summary.get("unknown_count"), int)
        and not isinstance(summary.get("unknown_count"), bool)
        and summary.get("unknown_count", -1) >= 0
    )
    commitment_shape_valid = all(
        isinstance(unsigned.get(field), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", unsigned[field])
        for field in ("observation_commitment", "target_commitment",
                      "result_commitment")
    )
    schema_valid = (
        unsigned.get("type") == "AgentGuildPreflightReceipt"
        and unsigned.get("contract") == CONTRACT
        and unsigned.get("transport") in {"http", "mcp", "a2a"}
        and bool(re.fullmatch(r"pfobs_[A-Za-z0-9_-]{40,50}", observation_id))
        and isinstance(unsigned.get("issued_at"), str)
        and proof.get("type") == "DataIntegrityProof"
        and proof.get("cryptosuite") == "eddsa-jcs-2022"
        and proof.get("proofPurpose") == "assertionMethod"
        and proof.get("created") == unsigned.get("issued_at")
        and proof.get("verificationMethod")
            == crypto.did_key_verification_method(issuer)
        and summary_shape_valid
        and commitment_shape_valid
    )
    try:
        public_key = crypto.public_key_from_did(issuer)
        signature_valid = (
            issuer_trusted
            and schema_valid
            and crypto.verify_eddsa_jcs(
                unsigned, {k: v for k, v in proof.items() if k != "proofValue"},
                str(proof.get("proofValue") or ""), public_key)
        )
    except (IndexError, TypeError, ValueError):
        signature_valid = False

    observation_commitment_valid = bool(observation_id) and (
        unsigned.get("observation_commitment")
        == _observation_commitment(observation_id)
    )
    target_match = None if target is None else (
        unsigned.get("target_commitment")
        == _private_commitment(observation_id, "target", {"url": target})
    )
    result_match = None if result is None else (
        unsigned.get("result_commitment")
        == _private_commitment(
            observation_id, "result", _result_without_receipt(result))
    )
    summary_match = None if result is None else summary == {
        "verdict": result.get("verdict"),
        "failed_count": len(result.get("failed") or []),
        "unknown_count": len(result.get("unknowns") or []),
    }
    valid = (signature_valid and observation_commitment_valid
             and target_match is not False and result_match is not False
             and summary_match is not False)
    return {
        "valid": valid,
        "signature_valid": signature_valid,
        "schema_valid": schema_valid,
        "issuer_trusted": issuer_trusted,
        "observation_commitment_valid": observation_commitment_valid,
        "exact_target_match": target_match,
        "exact_result_match": result_match,
        "result_summary_match": summary_match,
        "issuer": issuer or None,
        "trusted_issuer": trusted_issuer,
        "contract": unsigned.get("contract"),
        "semantics": (
            "A valid receipt proves Agent Guild issued these observation "
            "commitments. It does not prove caller identity, current safety, "
            "delegation authority, or payment authority."
        ),
    }
