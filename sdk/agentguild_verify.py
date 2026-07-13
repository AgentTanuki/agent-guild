"""Agent Guild — standalone AGI-1 Passport verifier (drop-in, ~zero dependencies).

This is the *verify-only* conformance path of the AGI-1 standard
(https://agent-guild-5d5r.onrender.com/standard): the lowest-friction way for ANY
AI agent or framework to check another agent's reputation — no Guild account, no SDK
lock-in, no server code. Copy this one file into your project (or `pip install
cryptography` and import it) and you can verify a Guild-signed Agent Passport
offline, then decide whether to delegate.

Why this exists: a standard is only a moat if it is trivially adoptable. Verifying a
counterparty's reputation should be one line. It is:

    >>> from agentguild_verify import vet
    >>> vet("agent_d0a8f6ef9b41")          # fetch + verify + decide
    {'recommendation': 'hire', 'trust': 44.2, 'verified': True, ...}

Only dependency: `cryptography` (for Ed25519). Everything else is stdlib. The crypto
mirrors the Guild's issuer exactly, so a Passport that verifies here is genuinely
Guild-signed — you are NOT trusting this code's author, you are checking a signature.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

DEFAULT_HOST = "https://agent-guild-5d5r.onrender.com"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# --- did:key (Ed25519) ------------------------------------------------------
def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    full = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + full


def public_key_from_did(did: str) -> bytes:
    """Extract the raw Ed25519 public key from a did:key string."""
    mb = did[len("did:key:"):] if did.startswith("did:key:") else did
    if not mb.startswith("z"):
        raise ValueError("unsupported did multibase")
    raw = _b58decode(mb[1:])
    if raw[0] != 0xED or raw[1] != 0x01:
        raise ValueError("did:key is not Ed25519")
    return raw[2:]


def _canonical(value: Any) -> str:
    """Language-agnostic JSON canonicalisation (AGI-1): sorted keys, no whitespace,
    ECMAScript number formatting (an integer-valued number carries no decimal point,
    e.g. 0.0 -> "0"). Must match the issuer byte-for-byte or signatures won't verify."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("NaN/Infinity not permitted")
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _canonical(v)
                              for k, v in sorted(value.items())) + "}"
    raise TypeError("not canonicalisable")


def _verify_sig(payload: Any, signature_hex: str, public_key: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            bytes.fromhex(signature_hex), _canonical(payload).encode("utf-8"))
        return True
    except (InvalidSignature, ValueError):
        return False


# --- credential verification (offline) --------------------------------------
def _b58_multibase_decode(s: str) -> bytes:
    if not s.startswith("z"):
        raise ValueError("not base58btc multibase")
    return _b58decode(s[1:])


def _verify_data_integrity(vc: dict[str, Any]) -> bool:
    """Conforming W3C Data Integrity, cryptosuite eddsa-jcs-2022
    (https://www.w3.org/TR/vc-di-eddsa/#eddsa-jcs-2022): JCS canonicalisation,
    hashData = SHA256(JCS(proofConfig)) || SHA256(JCS(document)), Ed25519,
    base58btc-multibase proofValue."""
    import hashlib
    proof = vc.get("proof") or {}
    if proof.get("cryptosuite") != "eddsa-jcs-2022":
        return False
    proof_value = proof.get("proofValue")
    if not proof_value:
        return False
    proof_config = {k: v for k, v in proof.items() if k != "proofValue"}
    document = {k: v for k, v in vc.items() if k != "proof"}
    if "@context" in proof_config and proof_config["@context"] != document.get("@context"):
        return False
    vm = proof.get("verificationMethod") or ""
    did = vm.split("#", 1)[0] if vm else vc.get("issuer", "")
    if vc.get("issuer") and did != vc["issuer"]:
        return False
    hash_data = (hashlib.sha256(_canonical(proof_config).encode("utf-8")).digest()
                 + hashlib.sha256(_canonical(document).encode("utf-8")).digest())
    try:
        Ed25519PublicKey.from_public_bytes(public_key_from_did(did)).verify(
            _b58_multibase_decode(proof_value), hash_data)
        return True
    except (InvalidSignature, ValueError):
        return False


def verify_credential(vc: dict[str, Any]) -> bool:
    """True iff `vc` carries a valid Ed25519 proof from its declared issuer DID.
    Pure, offline — no network. Tampering with any field breaks this.
    Handles both the conforming DataIntegrityProof (eddsa-jcs-2022) used for
    all current credentials and the immutable AGI-1 legacy format
    (hex signature, historical credentials only)."""
    try:
        proof = vc.get("proof") or {}
        if proof.get("type") == "DataIntegrityProof":
            return _verify_data_integrity(vc)
        proof_value = proof.get("proofValue")
        if not proof_value:
            return False
        payload = {k: v for k, v in vc.items() if k != "proof"}
        payload["proof"] = {k: v for k, v in proof.items() if k != "proofValue"}
        return _verify_sig(payload, proof_value, public_key_from_did(vc["issuer"]))
    except (KeyError, ValueError, TypeError):
        return False


def verify_checkpoint(cp: dict[str, Any]) -> bool:
    """True iff a signed ledger checkpoint carries a valid issuer signature. A
    checkpoint signs its body directly (its `proof` is a hex signature string, not a
    VC-style proof object), so it needs its own check."""
    try:
        proof = cp.get("proof")
        if not isinstance(proof, str):
            return False
        body = {k: v for k, v in cp.items() if k != "proof"}
        return _verify_sig(body, proof, public_key_from_did(cp["issuer"]))
    except (KeyError, ValueError, TypeError):
        return False


def verify_passport(vc: dict[str, Any], *, expected_issuer: Optional[str] = None) -> dict[str, Any]:
    """Verify an AGI-1 Agent Passport offline. Returns a structured result:
    {valid, issuer, subject, claims, checkpoint_valid, issuer_matches}.

    Pass `expected_issuer` (a DID) to pin a specific authority — e.g. the Guild's
    DID from GET /.well-known/agent-guild-did.json — so you only trust passports
    from an issuer you chose."""
    valid = verify_credential(vc)
    subj = (vc.get("credentialSubject") or {})
    issuer = vc.get("issuer", "")
    anchor = (subj.get("ledger_anchor") or {}) if valid else {}
    checkpoint = anchor.get("checkpoint") or {}
    checkpoint_valid = verify_checkpoint(checkpoint) if checkpoint else None
    return {
        "valid": valid,
        "issuer": issuer,
        "issuer_matches": (issuer == expected_issuer) if expected_issuer else None,
        "subject": subj.get("id", ""),
        "claims": {k: v for k, v in subj.items() if k != "id"} if valid else None,
        "verifiable_collaborations": anchor.get("verifiable_collaborations"),
        "checkpoint_valid": checkpoint_valid,
    }


# --- convenience: fetch + verify + decide (one call) ------------------------
def fetch_passport(agent_id: str, host: str = DEFAULT_HOST, timeout: float = 15.0) -> dict[str, Any]:
    with urllib.request.urlopen(f"{host}/agents/{agent_id}/passport", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def issuer_did(host: str = DEFAULT_HOST, timeout: float = 15.0) -> str:
    """The authority's signing DID (so you can pin it)."""
    with urllib.request.urlopen(f"{host}/.well-known/agent-guild-did.json", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))["did"]


def vet(agent_id: str, host: str = DEFAULT_HOST, *, pin_issuer: bool = True) -> dict[str, Any]:
    """One call: fetch `agent_id`'s Passport, verify it offline, and return a
    delegation decision. Set pin_issuer=False to skip pinning the Guild DID."""
    vc = fetch_passport(agent_id, host)
    expected = issuer_did(host) if pin_issuer else None
    res = verify_passport(vc, expected_issuer=expected)
    claims = res.get("claims") or {}
    trustworthy = bool(res["valid"] and (res["issuer_matches"] in (True, None)))
    return {
        "agent_id": agent_id,
        "verified": trustworthy,
        "recommendation": claims.get("recommendation") if trustworthy else None,
        "trust": claims.get("trust"),
        "risk": claims.get("risk"),
        "verifiable_collaborations": res.get("verifiable_collaborations"),
        "issuer": res["issuer"],
        "raw": res,
    }


if __name__ == "__main__":  # pragma: no cover
    import sys
    if len(sys.argv) < 2:
        print("usage: python agentguild_verify.py <agent_id> [host]")
        raise SystemExit(2)
    host = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_HOST
    print(json.dumps(vet(sys.argv[1], host), indent=2))
