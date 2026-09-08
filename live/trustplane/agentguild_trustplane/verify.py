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


SUPPORTED_PROOF_PURPOSE = "assertionMethod"

#: Tolerance for issuer/verifier clock skew on the START of a validity
#: window only: a document that starts up to this many seconds in the future
#: is treated as already valid. Anything further ahead is not valid yet.
CLOCK_SKEW_SECONDS = 60.0


_ABSENT = object()


def _document_issuer(doc: dict[str, Any]) -> Any:
    """The issuer the DOCUMENT claims: VC ``issuer`` (non-empty string or
    {"id": <non-empty string>, ...}) or the Guild decision envelope's
    ``issuer`` string. Returns ``_ABSENT`` when the key is absent, the
    string when well-formed, and raises ValueError when present but
    malformed (None, number, list, empty, object without a string id)."""
    if "issuer" not in doc:
        return _ABSENT
    iss = doc["issuer"]
    if isinstance(iss, dict):
        iss = iss.get("id")
    if not isinstance(iss, str) or not iss:
        raise ValueError("issuer present but malformed")
    return iss


def _is_credential(doc: dict[str, Any]) -> bool:
    """VC profile (passports, attestations): has a credentialSubject or a
    VerifiableCredential type. Decision envelopes are not credentials."""
    if "credentialSubject" in doc:
        return True
    types = doc.get("type")
    if isinstance(types, str):
        types = [types]
    return isinstance(types, list) and "VerifiableCredential" in types


def _controller_from_verification_method(vm: Any) -> str:
    """The did:key controller named by ``verificationMethod`` under this
    verifier's profile: ``did:key:<multibase>`` optionally followed by
    ``#<multibase>`` (the did:key convention — the fragment must equal the
    key). Raises ValueError for any other shape."""
    if not isinstance(vm, str) or not vm.startswith("did:key:z"):
        raise ValueError("verificationMethod is not a did:key")
    controller, sep, fragment = vm.partition("#")
    if sep and fragment != controller[len("did:key:"):]:
        raise ValueError("verificationMethod fragment does not name the did:key")
    return controller


def verify_data_integrity(signed_doc: dict[str, Any],
                          expected_issuer_did: Optional[str] = None) -> dict[str, Any]:
    """Verify an eddsa-jcs-2022 DataIntegrityProof on ``signed_doc``.

    Returns {"verified": bool, "reason": str, "issuer_did": str|None}.
    Never raises on bad input — a malformed document is an unverified one.

    Profile (deliberately narrow — this is the Guild's issuing profile, not
    a general W3C Data Integrity verifier): ``proof.type`` DataIntegrityProof,
    ``cryptosuite`` eddsa-jcs-2022, ``proofPurpose`` assertionMethod,
    ``verificationMethod`` a did:key Ed25519 controller (``did:key:z…`` with
    an optional ``#z…`` fragment equal to the key), ``proofValue`` base58btc
    multibase, ``proof.@context`` (when present) equal to the document's.
    The document's own ``issuer`` (VC ``issuer`` / envelope ``issuer``) MUST,
    when present, be a non-empty string (or ``{"id": <string>}``) equal to
    the signing controller — a credential that names one issuer and is
    signed by another, or carries a malformed issuer, does not verify. A
    credential (``credentialSubject`` / ``VerifiableCredential`` type) must
    name an issuer; a decision envelope may omit it (historical shape). Validity windows and
    subject binding are separate checks (``within_validity``, client), and
    whether the controller is a TRUSTED issuer is policy, decided by the
    caller (``expected_issuer_did``, cache pins, RiskPolicy.trusted_issuers).
    """
    try:
        doc = dict(signed_doc)
        proof = dict(doc.pop("proof"))
        if proof.get("type") != "DataIntegrityProof" or \
           proof.get("cryptosuite") != "eddsa-jcs-2022":
            return {"verified": False, "reason": "unsupported proof suite",
                    "issuer_did": None}
        if proof.get("proofPurpose") != SUPPORTED_PROOF_PURPOSE:
            return {"verified": False,
                    "reason": f"unsupported proofPurpose: {proof.get('proofPurpose')!r}",
                    "issuer_did": None}
        if "@context" in proof and proof["@context"] != doc.get("@context"):
            return {"verified": False, "reason": "proof @context mismatch",
                    "issuer_did": None}
        try:
            issuer_did = _controller_from_verification_method(
                proof.get("verificationMethod"))
            pub = Ed25519PublicKey.from_public_bytes(public_key_from_did(issuer_did))
        except ValueError as e:
            return {"verified": False, "reason": f"malformed: {e}",
                    "issuer_did": None}
        try:
            claimed = _document_issuer(doc)
        except ValueError as e:
            return {"verified": False, "reason": f"malformed: {e}",
                    "issuer_did": issuer_did}
        if claimed is _ABSENT and _is_credential(doc):
            # VC profile: a credential must name its issuer. Decision
            # envelopes historically omit the field and remain accepted.
            return {"verified": False, "reason": "credential has no issuer",
                    "issuer_did": issuer_did}
        if claimed is not _ABSENT and claimed != issuer_did:
            return {"verified": False,
                    "reason": f"document issuer {claimed} is not the signing "
                              f"controller {issuer_did}",
                    "issuer_did": issuer_did}
        if expected_issuer_did and issuer_did != expected_issuer_did:
            return {"verified": False,
                    "reason": f"issuer mismatch: {issuer_did}",
                    "issuer_did": issuer_did}
        proof_value = proof.pop("proofValue")
        if not isinstance(proof_value, str) or not proof_value.startswith("z"):
            return {"verified": False, "reason": "proofValue not base58btc",
                    "issuer_did": issuer_did}
        sig = b58decode(proof_value[1:])
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


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:          # a naive timestamp is not a validity bound
        return None
    return t


def within_validity(doc: dict[str, Any],
                    now: Optional[datetime] = None) -> tuple[bool, Optional[float]]:
    """(valid_now, age_seconds) from issued_at/valid_until (or validFrom/
    validUntil for VCs).

    Valid only when ``start <= now <= end`` (start tolerating
    ``CLOCK_SKEW_SECONDS`` of issuer clock lead) and the window itself is
    sane (``start <= end``). A document whose start is further in the
    FUTURE is not valid yet (age is negative); an expired one is not valid
    any more; missing, unparsable or timezone-naive bounds -> (False, None):
    an unbounded assertion is not acceptable offline evidence."""
    now = now or datetime.now(timezone.utc)
    t0 = _parse_ts(doc.get("issued_at") or doc.get("validFrom"))
    t1 = _parse_ts(doc.get("valid_until") or doc.get("validUntil"))
    if t0 is None or t1 is None:
        return False, None
    if t1 < t0:
        return False, None
    age = (now - t0).total_seconds()
    started = age >= -CLOCK_SKEW_SECONDS
    return (started and now <= t1), age
