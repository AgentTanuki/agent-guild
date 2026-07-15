"""Real ed25519 cryptography + did:key identity for the live Agent Guild service.

Mirrors the TypeScript prototype's crypto layer: ed25519 keypairs, did:key
encoding, deterministic JSON canonicalisation, and sign/verify. No blockchain.
"""
from __future__ import annotations

import json
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature

# --- base58btc (Bitcoin alphabet) ------------------------------------------
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    # preserve leading zero bytes as '1'
    pad = 0
    for b in data:
        if b == 0:
            pad += 1
        else:
            break
    return "1" * pad + out


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    full = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = 0
    for ch in s:
        if ch == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + full


# --- keys -------------------------------------------------------------------
def generate_keypair() -> tuple[str, str]:
    """Return (private_hex, public_hex) for a fresh ed25519 keypair."""
    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes_raw()
    pub_raw = priv.public_key().public_bytes_raw()
    return priv_raw.hex(), pub_raw.hex()


# did:key multicodec prefix for ed25519-pub is 0xed 0x01.
_ED25519_MULTICODEC = bytes([0xED, 0x01])


def did_from_public_key(public_hex: str) -> str:
    pub = bytes.fromhex(public_hex)
    prefixed = _ED25519_MULTICODEC + pub
    return "did:key:z" + b58encode(prefixed)


def public_key_from_did(did: str) -> str:
    mb = did[len("did:key:"):] if did.startswith("did:key:") else did
    if not mb.startswith("z"):
        raise ValueError("unsupported did multibase")
    raw = b58decode(mb[1:])
    if raw[0] != 0xED or raw[1] != 0x01:
        raise ValueError("did:key is not ed25519")
    return raw[2:].hex()


# --- canonicalisation + signing --------------------------------------------
def canonicalize(value: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace. (Pragmatic stand-in for
    full JSON-LD canonicalisation; stable and sufficient for this service.)"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sign_payload(payload: Any, private_hex: str) -> str:
    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    sig = priv.sign(canonicalize(payload).encode("utf-8"))
    return sig.hex()


def verify_payload(payload: Any, signature_hex: str, public_hex: str) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        pub.verify(bytes.fromhex(signature_hex), canonicalize(payload).encode("utf-8"))
        return True
    except (InvalidSignature, ValueError):
        return False


# --- language-agnostic canonicalisation (AGI-1 signatures) ------------------
# Python's json.dumps serialises an integer-valued float as "0.0", but ECMAScript
# (JavaScript) produces "0". For a credential signature to verify in ANY language,
# the canonical bytes must be reproducible everywhere. This canonicaliser matches
# the ECMAScript JSON number rule (integer-valued numbers carry no decimal point),
# so a Guild-signed Verifiable Credential verifies identically in Python, JS, Go, …
def _jcs_number(x: Any) -> str:
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, int):
        return str(x)
    f = float(x)
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError("NaN/Infinity not permitted in canonical JSON")
    if f.is_integer():
        return str(int(f))          # 0.0 -> "0", 44.0 -> "44"  (matches JS)
    return repr(f)                  # 44.2 -> "44.2"            (shortest round-trip)


def canonicalize_jcs(value: Any) -> str:
    """Deterministic, language-agnostic JSON (sorted keys, no whitespace,
    ECMAScript number formatting). The canonical form AGI-1 signs over."""
    if value is None:
        return "null"
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return _jcs_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonicalize_jcs(v) for v in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: kv[0])
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canonicalize_jcs(v)
                              for k, v in items) + "}"
    raise TypeError(f"not JSON-canonicalisable: {type(value)}")


def sign_jcs(payload: Any, private_hex: str) -> str:
    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    return priv.sign(canonicalize_jcs(payload).encode("utf-8")).hex()


def verify_jcs(payload: Any, signature_hex: str, public_hex: str) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        pub.verify(bytes.fromhex(signature_hex), canonicalize_jcs(payload).encode("utf-8"))
        return True
    except (InvalidSignature, ValueError):
        return False


# --- W3C Data Integrity: eddsa-jcs-2022 -------------------------------------
# The CONFORMING cryptosuite (https://www.w3.org/TR/vc-di-eddsa/#eddsa-jcs-2022):
#   * canonicalisation: JCS (RFC 8785) — our canonicalize_jcs implements it
#   * hashData = SHA256(JCS(proofConfig)) || SHA256(JCS(document-without-proof))
#   * signature: raw Ed25519 over hashData; proofValue: base58btc multibase
# This replaces the previous MISLABELED use of the name "Ed25519Signature2020"
# (whose spec requires RDF canonicalisation we never performed). Historical
# credentials keep their bytes and verify through the legacy path in vc.py.
import hashlib as _hashlib


def multibase_b58btc(data: bytes) -> str:
    """base58btc multibase ('z' prefix) — Data Integrity proofValue encoding."""
    return "z" + b58encode(data)


def multibase_b58btc_decode(s: str) -> bytes:
    if not s.startswith("z"):
        raise ValueError("not base58btc multibase")
    return b58decode(s[1:])


def did_key_verification_method(did: str) -> str:
    """The conforming did:key verification method id: did:key:zMB#zMB."""
    mb = did[len("did:key:"):] if did.startswith("did:key:") else did
    return f"did:key:{mb}#{mb}"


# --- did:web (W3C did:web method spec, https://w3c-ccg.github.io/did-method-web/)
# The DID is derived from the https origin: the domain is the method-specific
# identifier; an explicit port's colon is percent-encoded (%3A, spec §3.2);
# path segments become colon-separated identifier parts. Resolution fetches
# {origin}/.well-known/did.json (domain-only DIDs, spec §3.2 read-operation).


def did_web_from_origin(origin: str) -> str:
    """did:web DID for an https (or localhost http) origin.
    'https://example.com'        → did:web:example.com
    'http://localhost:8123'      → did:web:localhost%3A8123
    'https://example.com/a/b'    → did:web:example.com:a:b
    """
    from urllib.parse import urlparse, quote
    p = urlparse(origin if "://" in origin else "https://" + origin)
    host = p.hostname or ""
    ident = quote(host, safe="")
    if p.port:
        ident += "%3A" + str(p.port)
    path = (p.path or "").strip("/")
    if path:
        ident += ":" + ":".join(quote(seg, safe="")
                                for seg in path.split("/"))
    return "did:web:" + ident


def public_key_multibase(public_hex: str) -> str:
    """Multicodec-prefixed base58btc multibase of an Ed25519 public key —
    the exact string that is both the did:key identifier body and a
    conforming `publicKeyMultibase` value (Multikey)."""
    return "z" + b58encode(_ED25519_MULTICODEC + bytes.fromhex(public_hex))


def did_web_verification_method(origin: str, public_hex: str) -> str:
    """The did:web verification-method DID URL for the service signing key:
    did:web:<origin-identifier>#<multibase-of-the-key>. The fragment is the
    key's own multibase so the binding is self-describing and collision-free
    across rotations."""
    return f"{did_web_from_origin(origin)}#{public_key_multibase(public_hex)}"


def eddsa_jcs_hash_data(document: Any, proof_config: dict[str, Any]) -> bytes:
    cfg = _hashlib.sha256(canonicalize_jcs(proof_config).encode("utf-8")).digest()
    doc = _hashlib.sha256(canonicalize_jcs(document).encode("utf-8")).digest()
    return cfg + doc


def sign_eddsa_jcs(document: Any, proof_config: dict[str, Any],
                   private_hex: str) -> str:
    """Sign per eddsa-jcs-2022; returns the multibase proofValue."""
    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    sig = priv.sign(eddsa_jcs_hash_data(document, proof_config))
    return multibase_b58btc(sig)


def verify_eddsa_jcs(document: Any, proof_config: dict[str, Any],
                     proof_value: str, public_hex: str) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        pub.verify(multibase_b58btc_decode(proof_value),
                   eddsa_jcs_hash_data(document, proof_config))
        return True
    except (InvalidSignature, ValueError):
        return False
