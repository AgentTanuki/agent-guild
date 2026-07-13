"""W3C Verifiable Credentials for attestations and passports.

An attestation is a signed VC: issuer DID asserts a quality rating about a
subject DID for a given capability/task. A passport is a Guild-signed
reputation snapshot. Both verify offline against the issuer's did:key.

PROOF SUITES (2026-07-13 production-truth fix):

* NEW credentials carry a CONFORMING W3C Data Integrity proof:
  `DataIntegrityProof` with cryptosuite `eddsa-jcs-2022`
  (https://www.w3.org/TR/vc-di-eddsa/#eddsa-jcs-2022) — JCS (RFC 8785)
  canonicalisation, hashData = SHA256(JCS(proofConfig)) || SHA256(JCS(doc)),
  raw Ed25519, base58btc-multibase proofValue, did:key verification method.
  Independently verifiable with standard third-party VC tooling (tested
  against non-Guild implementations in Python and Node — see
  verifiers/ in the repo root).

* HISTORICAL credentials were signed with a hex-encoded Ed25519 signature over
  a JCS-style canonicalisation of the whole credential (proof sans proofValue
  embedded). That format was MISLABELED "Ed25519Signature2020" — the real
  Ed25519Signature2020 spec requires RDF canonicalisation this service never
  performed. Those bytes are immutable, so verification keeps a legacy path
  for them, documented as the **AGI-1 legacy proof** (docs/PROOF_SUITES.md).
  No new credential is ever issued in the legacy format.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .crypto import (sign_jcs, verify_jcs, public_key_from_did,
                     did_key_verification_method, sign_eddsa_jcs,
                     verify_eddsa_jcs)

# Legacy context retained so historical credentials' bytes stay reproducible.
VC_CONTEXT = [
    "https://www.w3.org/ns/credentials/v2",
    "https://w3id.org/security/suites/ed25519-2020/v1",
]
# The VC 2.0 context defines DataIntegrityProof/proof terms natively.
VC_CONTEXT_V2 = ["https://www.w3.org/ns/credentials/v2"]

DATA_INTEGRITY_CRYPTOSUITE = "eddsa-jcs-2022"
LEGACY_PROOF_TYPE = "Ed25519Signature2020"   # verify-only; never issued anymore


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _add_data_integrity_proof(unsigned: dict[str, Any], issuer_did: str,
                              issuer_private_hex: str, created: str) -> dict[str, Any]:
    """Attach a conforming eddsa-jcs-2022 DataIntegrityProof to `unsigned`
    (which must NOT contain a `proof` member)."""
    proof: dict[str, Any] = {}
    if "@context" in unsigned:
        # spec: proof.@context mirrors the secured document's @context
        proof["@context"] = unsigned["@context"]
    proof.update({
        "type": "DataIntegrityProof",
        "cryptosuite": DATA_INTEGRITY_CRYPTOSUITE,
        "created": created,
        "verificationMethod": did_key_verification_method(issuer_did),
        "proofPurpose": "assertionMethod",
    })
    proof["proofValue"] = sign_eddsa_jcs(unsigned, proof, issuer_private_hex)
    secured = dict(unsigned)
    secured["proof"] = proof
    return secured


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
        "@context": VC_CONTEXT_V2,
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
    }
    return _add_data_integrity_proof(unsigned, issuer_did, issuer_private_hex, created)


def issue_passport(
    *,
    cred_id: str,
    issuer_did: str,
    issuer_private_hex: str,
    subject_did: str,
    subject_claims: dict[str, Any],
    valid_from: str | None = None,
    valid_until: str | None = None,
) -> dict[str, Any]:
    """Issue a portable, Guild-signed **Agent Passport** — a Verifiable
    Credential summarising an agent's reputation, verifiable offline against
    the Guild's did:key with any conforming eddsa-jcs-2022 verifier."""
    created = valid_from or _now_iso()
    unsigned: dict[str, Any] = {
        "@context": VC_CONTEXT_V2,
        "id": cred_id,
        "type": ["VerifiableCredential", "AgentGuildPassport"],
        "issuer": issuer_did,
        "validFrom": created,
    }
    if valid_until:
        unsigned["validUntil"] = valid_until
    unsigned["credentialSubject"] = {"id": subject_did, **subject_claims}
    return _add_data_integrity_proof(unsigned, issuer_did, issuer_private_hex, created)


def _verify_data_integrity(vc: dict[str, Any]) -> bool:
    proof = vc.get("proof") or {}
    if proof.get("cryptosuite") != DATA_INTEGRITY_CRYPTOSUITE:
        return False
    proof_value = proof.get("proofValue")
    if not proof_value:
        return False
    proof_config = {k: v for k, v in proof.items() if k != "proofValue"}
    document = {k: v for k, v in vc.items() if k != "proof"}
    # conforming check: the proof's @context (if present) must match the doc's
    if "@context" in proof_config and proof_config["@context"] != document.get("@context"):
        return False
    vm = proof.get("verificationMethod") or ""
    did = vm.split("#", 1)[0] if vm else vc.get("issuer", "")
    # the verification method must belong to the issuer — a proof signed with
    # someone else's key must not verify as the issuer's assertion
    if vc.get("issuer") and did != vc["issuer"]:
        return False
    issuer_pub = public_key_from_did(did)
    return verify_eddsa_jcs(document, proof_config, proof_value, issuer_pub)


def _verify_legacy_agi1(vc: dict[str, Any]) -> bool:
    """AGI-1 legacy proof (mislabeled Ed25519Signature2020): hex Ed25519 over
    JCS of the credential with proof-sans-proofValue embedded. Verify-only."""
    proof = vc.get("proof") or {}
    proof_value = proof.get("proofValue")
    if not proof_value:
        return False
    proof_without_value = {k: v for k, v in proof.items() if k != "proofValue"}
    payload = {k: v for k, v in vc.items() if k != "proof"}
    payload["proof"] = proof_without_value
    issuer_pub = public_key_from_did(vc["issuer"])
    return verify_jcs(payload, proof_value, issuer_pub)


def verify_credential(vc: dict[str, Any]) -> bool:
    """Verify a Guild credential of either era: conforming DataIntegrityProof
    (eddsa-jcs-2022) for everything issued now, or the immutable historical
    AGI-1 legacy format (verify-only)."""
    try:
        proof = vc.get("proof") or {}
        ptype = proof.get("type")
        if ptype == "DataIntegrityProof":
            return _verify_data_integrity(vc)
        if ptype == LEGACY_PROOF_TYPE:
            return _verify_legacy_agi1(vc)
        return False
    except (KeyError, ValueError, TypeError):
        return False
