"""A2A Agent Card signing (A2A spec §8.4, JWS per RFC 7515).

Why this exists: our own /preflight rates any A2A endpoint whose card carries
no `signatures` as `delegate_with_caution`. Until 2026-09-24 that included us —
the Guild did not meet the bar it asks of everyone else. This module signs the
card we serve with the Guild's persistent Ed25519 SERVICE-signing key (the same
key behind did:web /.well-known/did.json and x402 offers/receipts).

What is signed (spec §8.4.1): the served card JSON minus the `signatures`
member, canonicalised with RFC 8785 JCS. We do not emit any field we would
have to strip as a proto default, so "served JSON minus signatures" and the
spec's proto-presence form coincide for this card. Vendor `x-…` members are
part of the signed content: a verifier that rebuilds the card from the proto
schema and drops unknown members will not reproduce the payload — that is a
property of every card with vendor extensions, and is stated in the
documentation, not hidden.

Protected header: {"alg":"EdDSA","typ":"JOSE","kid":<did:web key URL>}.
`kid` resolves through the DID document (/.well-known/did.json); the same
key is also published as an OKP JWK at /.well-known/jwks.json for generic
JOSE verifiers (kid-matched).
"""
from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from . import crypto

JWKS_PATH = "/.well-known/jwks.json"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def canonical_payload(card: dict[str, Any]) -> bytes:
    """RFC 8785 canonical form of the card with `signatures` excluded."""
    unsigned = {k: v for k, v in card.items() if k != "signatures"}
    return crypto.canonicalize_jcs(unsigned).encode("utf-8")


def jwk_for_identity(identity: dict[str, Any], origin: str) -> dict[str, Any]:
    """The service signing key as an OKP/Ed25519 JWK (RFC 8037)."""
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": _b64url(bytes.fromhex(identity["public_key"])),
        "kid": crypto.did_web_verification_method(origin, identity["public_key"]),
        "use": "sig",
        "alg": "EdDSA",
    }


def jwks_document(identity: dict[str, Any], origin: str) -> dict[str, Any]:
    return {"keys": [jwk_for_identity(identity, origin)]}


def sign_agent_card(card: dict[str, Any], identity: dict[str, Any],
                    origin: str) -> dict[str, Any]:
    """Return a copy of `card` with one AgentCardSignature appended."""
    kid = crypto.did_web_verification_method(origin, identity["public_key"])
    # `jku` is optional in the spec and the kid resolves through the did:web
    # document; leaving it out keeps the largest card variant inside the
    # 5 KiB registry-compatibility budget. The same key is still published
    # at /.well-known/jwks.json for generic JOSE verifiers.
    header = {"alg": "EdDSA", "typ": "JOSE", "kid": kid}
    protected = _b64url(json.dumps(header, separators=(",", ":"),
                                   sort_keys=True).encode("utf-8"))
    signing_input = protected + "." + _b64url(canonical_payload(card))
    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(identity["private_key"]))
    sig = priv.sign(signing_input.encode("ascii"))
    signed = {k: v for k, v in card.items() if k != "signatures"}
    signed["signatures"] = [{"protected": protected, "signature": _b64url(sig)}]
    return signed


def verify_agent_card(card: dict[str, Any], public_hex: str) -> dict[str, Any] | None:
    """Verify the first signature on `card` against an Ed25519 public key.
    Returns the decoded protected header on success, None otherwise. This is
    the spec §8.4.3 procedure: strip `signatures`, JCS-canonicalise, verify
    the JWS signing input."""
    sigs = card.get("signatures")
    if not isinstance(sigs, list) or not sigs:
        return None
    entry = sigs[0]
    try:
        header = json.loads(_b64url_decode(entry["protected"]))
        if header.get("alg") != "EdDSA":
            return None
        signing_input = entry["protected"] + "." + _b64url(canonical_payload(card))
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        pub.verify(_b64url_decode(entry["signature"]), signing_input.encode("ascii"))
        return header
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return None
