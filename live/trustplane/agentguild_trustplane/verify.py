"""Standalone verifier for Guild-signed documents (decisions, passports).

Implements the eddsa-jcs-2022 Data Integrity cryptosuite over did:key
(Ed25519) with RFC 8785-style JCS canonicalisation, byte-compatible with the
Guild issuer (live/guild/app/crypto.py) but with ZERO imports from it — this
file is the independence boundary: a decision that verifies here is verified
by code that never touches Guild internals. Only dependency: ``cryptography``.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    full = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + full


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = 0
    for b in data:
        if b == 0:
            pad += 1
        else:
            break
    return "1" * pad + out


def public_key_from_did(did: str) -> bytes:
    """Raw Ed25519 public key bytes from a did:key string."""
    mb = did[len("did:key:"):] if did.startswith("did:key:") else did
    mb = mb.split("#")[0]
    if not mb.startswith("z"):
        raise ValueError("unsupported did multibase")
    raw = b58decode(mb[1:])
    if raw[:2] != b"\xed\x01":
        raise ValueError("did:key is not Ed25519")
    return raw[2:]


def _num(x: Any) -> str:
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, int):
        return str(x)
    f = float(x)
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError("NaN/Infinity not permitted")
    return str(int(f)) if f.is_integer() else repr(f)


def canonicalize_jcs(value: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, ECMAScript numbers."""
    if value is None:
        return "null"
    if isinstance(value, (bool, int, float)):
        return _num(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonicalize_jcs(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(k, ensure_ascii=False) + ":" + canonicalize_jcs(v)
            for k, v in sorted(value.items(), key=lambda kv: kv[0])) + "}"
    raise TypeError(f"not canonicalisable: {type(value)}")


def _hash_data(document: Any, proof_config: dict[str, Any]) -> bytes:
    cfg = hashlib.sha256(canonicalize_jcs(proof_config).encode()).digest()
    doc = hashlib.sha256(canonicalize_jcs(document).encode()).digest()
    return cfg + doc


def verify_data_integrity(signed_doc: dict[str, Any],
                          expected_issuer_did: Optional[str] = None) -> dict[str, Any]:
    """Verify an eddsa-jcs-2022 DataIntegrityProof on ``signed_doc``.

    Returns {"verified": bool, "reason": str, "issuer_did": str|None}.
    Never raises on bad input — a malformed document is an unverified one.
    """
    try:
        doc = dict(signed_doc)
        proof = dict(doc.pop("proof"))
        if proof.get("type") != "DataIntegrityProof" or \
           proof.get("cryptosuite") != "eddsa-jcs-2022":
            return {"verified": False, "reason": "unsupported proof suite",
                    "issuer_did": None}
        vm = proof.get("verificationMethod", "")
        issuer_did = vm.split("#")[0]
        if expected_issuer_did and issuer_did != expected_issuer_did:
            return {"verified": False,
                    "reason": f"issuer mismatch: {issuer_did}",
                    "issuer_did": issuer_did}
        proof_value = proof.pop("proofValue")
        if not proof_value.startswith("z"):
            return {"verified": False, "reason": "proofValue not base58btc",
                    "issuer_did": issuer_did}
        sig = b58decode(proof_value[1:])
        pub = Ed25519PublicKey.from_public_bytes(public_key_from_did(issuer_did))
        pub.verify(sig, _hash_data(doc, proof))
        return {"verified": True, "reason": "ok", "issuer_did": issuer_did}
    except InvalidSignature:
        return {"verified": False, "reason": "signature invalid",
                "issuer_did": None}
    except Exception as e:  # malformed input
        return {"verified": False, "reason": f"malformed: {e}",
                "issuer_did": None}


def verify_jcs_hex(payload: Any, signature_hex: str, did: str) -> bool:
    """Verify a bare hex ed25519 signature over the JCS canonical form of
    ``payload`` against a did:key — the Guild's ledger-entry signature format
    (issuer rotations, feed entry proofs). Independent implementation."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(public_key_from_did(did))
        pub.verify(bytes.fromhex(signature_hex),
                   canonicalize_jcs(payload).encode("utf-8"))
        return True
    except Exception:
        return False


def verify_rotation_chain(pinned_did: str, target_did: str,
                          rotation_entries: list[dict[str, Any]]) -> bool:
    """True iff a VERIFIED issuer-rotation chain connects ``pinned_did`` to
    ``target_did``. Each link must be dual-signed over the same core
    ({old_did, new_did, rotated_at}): the OLD key endorses the successor and
    the NEW key proves possession. Any unverifiable or discontinuous link
    fails the whole chain — a changed issuer without this proof is rejected."""
    if pinned_did == target_did:
        return True
    did = pinned_did
    for entry in rotation_entries:
        body = entry.get("body") or entry
        try:
            core = {k: body[k] for k in ("old_did", "new_did", "rotated_at")}
        except KeyError:
            return False
        if body.get("old_did") != did:
            return False
        if not (verify_jcs_hex(core, body.get("proof_old_key", ""),
                               body["old_did"])
                and verify_jcs_hex(core, body.get("proof_new_key", ""),
                                   body["new_did"])):
            return False
        did = body["new_did"]
        if did == target_did:
            return True
    return did == target_did


def within_validity(doc: dict[str, Any],
                    now: Optional[datetime] = None) -> tuple[bool, Optional[float]]:
    """(still_valid, age_seconds) from issued_at/valid_until (or validFrom/
    validUntil for VCs). Missing validity metadata -> (False, None): an
    unbounded assertion is not acceptable offline evidence."""
    now = now or datetime.now(timezone.utc)
    issued = doc.get("issued_at") or doc.get("validFrom")
    until = doc.get("valid_until") or doc.get("validUntil")
    if not issued or not until:
        return False, None
    try:
        t0 = datetime.fromisoformat(issued.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(until.replace("Z", "+00:00"))
    except ValueError:
        return False, None
    age = (now - t0).total_seconds()
    return (now <= t1), age
