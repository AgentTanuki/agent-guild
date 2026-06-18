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
