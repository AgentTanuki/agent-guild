"""W3C Verifiable Credentials for attestations.

An attestation is a signed VC: issuer DID asserts a quality rating about a
subject DID for a given capability/task. Signed with ed25519, verifiable
offline against the issuer's did:key.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .crypto import sign_payload, verify_payload, public_key_from_did

VC_CONTEXT = [
    "https://www.w3.org/ns/credentials/v2",
    "https://w3id.org/security/suites/ed25519-2020/v1",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def issue_credential(
    *,
    cred_id: str,
    types: list[str],
    issuer_did: str,
    issuer_private_hex: str,
    subject_did: str,
    capability: str,
    rating: float,
    task_id: str = "n/a",
    comment: str = "",
    timestamp: str | None = None,
) -> dict[str, Any]:
    created = timestamp or _now_iso()
    unsigned = {
        "@context": VC_CONTEXT,
        "id": cred_id,
        "type": ["VerifiableCredential", *types],
        "issuer": issuer_did,
        "validFrom": created,
        "credentialSubject": {
            "id": subject_did,
            "capability": capability,
            "rating": rating,
            "taskId": task_id,
            "comment": comment,
        },
        "proof": {
            "type": "Ed25519Signature2020",
            "created": created,
            "verificationMethod": f"{issuer_did}#{issuer_did.split(':')[-1]}",
            "proofPurpose": "assertionMethod",
        },
    }
    proof_value = sign_payload(unsigned, issuer_private_hex)
    cred = dict(unsigned)
    cred["proof"] = {**unsigned["proof"], "proofValue": proof_value}
    return cred


def verify_credential(vc: dict[str, Any]) -> bool:
    """Recompute the signed payload and verify against the issuer's did:key."""
    try:
        proof = vc.get("proof") or {}
        proof_value = proof.get("proofValue")
        if not proof_value:
            return False
        proof_without_value = {k: v for k, v in proof.items() if k != "proofValue"}
        payload = {k: v for k, v in vc.items() if k != "proof"}
        payload["proof"] = proof_without_value
        issuer_pub = public_key_from_did(vc["issuer"])
        return verify_payload(payload, proof_value, issuer_pub)
    except (KeyError, ValueError):
        return False
