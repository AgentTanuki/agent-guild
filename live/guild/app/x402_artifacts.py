"""x402 signed offers + receipts (official offer/receipt extension) and the
Agent Guild evidence attachment.

Implements the x402 "Offer and Receipt Extension" (specs/extensions/
extension-offer-and-receipt.md, payload schema version 1) in the JWS format:

  * **Signed offer** — the server's cryptographic commitment to the payment
    terms in one `accepts[]` entry, placed at
    ``extensions["offer-receipt"].info.offers[]`` of the 402 challenge.
  * **Signed receipt** — issued ONLY on a successful, served payment, placed at
    ``extensions["offer-receipt"].info.receipt`` of the SettleResponse
    (PAYMENT-RESPONSE header / A2A receipts array / MCP payment-response meta).

Signing identity — load-bearing decisions:
  * The signer is the Guild's PERSISTENT Ed25519 signing identity
    (store.guild_identity(), the same did:key that signs Agent Passports and
    AGD-1 decisions). It is a dedicated SERVICE-signing identity: it is not,
    and must never be, the treasury key (the treasury is an EVM account whose
    key lives only in CDP; this module never touches EVM keys).
  * Format is JWS (`alg: EdDSA`, `kid: <did:key…>#<fragment>`) — the spec's
    JWS branch accepts EdDSA and requires `kid` to be a DID URL, which did:key
    satisfies without weakening the existing AGI-1/did:key verification model.
    did:web is NOT required by the spec (§4.5.1 allows an external key
    registry); the key binding for the resource origin is published at
    ``/.well-known/agent-guild-did.json`` (and mirrored in the x402 section of
    ``/.well-known/agent-guild.json``), so an independent verifier can bind
    resourceUrl origin → kid → Ed25519 key without trusting the 402 itself.

The Agent Guild evidence attachment is a SIBLING extension
(``extensions["io.agent-guild/evidence"]``) so the standard `offer-receipt`
fields are never altered: it carries the response hash, the request hash, and
the AGI-1 checkpoint pin, each inside its own Guild-signed JWS.

Verification of every artifact produced here is exercised by an INDEPENDENT
official TypeScript verifier (@x402/extensions — verifyOfferSignatureJWS /
verifyReceiptSignatureJWS with did:key resolution) in
tests/test_signed_offer_receipt.py; do not claim standards compliance from the
Python self-checks alone.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from . import crypto

OFFER_RECEIPT_EXTENSION = "offer-receipt"
EVIDENCE_EXTENSION = "io.agent-guild/evidence"
PAYLOAD_VERSION = 1          # offer/receipt payload schema version (spec §4.2/§5.2)
OFFER_TTL_SECONDS = 300      # matches maxTimeoutSeconds in the quoted accepts


# --- JWS (compact, EdDSA/Ed25519) --------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def jws_sign(payload: dict[str, Any], private_hex: str, kid: str) -> str:
    """Compact JWS, `alg: EdDSA`, `kid` = DID URL. The payload is serialized
    once and signed as those exact bytes — verifiers never re-canonicalize."""
    header = {"alg": "EdDSA", "kid": kid}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    )
    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    sig = priv.sign(signing_input.encode("ascii"))
    return signing_input + "." + _b64url(sig)


def jws_header(jws: str) -> dict[str, Any]:
    return json.loads(_b64url_decode(jws.split(".")[0]))


def jws_payload(jws: str) -> dict[str, Any]:
    return json.loads(_b64url_decode(jws.split(".")[1]))


def jws_verify(jws: str, public_hex: str) -> Optional[dict[str, Any]]:
    """Verify a compact EdDSA JWS against an Ed25519 public key. Returns the
    payload on success, None on ANY failure (tampered, wrong key, malformed)."""
    try:
        h_b64, p_b64, s_b64 = jws.split(".")
        header = json.loads(_b64url_decode(h_b64))
        if header.get("alg") != "EdDSA":
            return None
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        pub.verify(_b64url_decode(s_b64), (h_b64 + "." + p_b64).encode("ascii"))
        return json.loads(_b64url_decode(p_b64))
    except (ValueError, KeyError, InvalidSignature):
        return None


def kid_for_identity(identity: dict[str, Any]) -> str:
    """DID URL for the Guild signing key: did:key:zMB#zMB (the did:key
    convention the official TS verifier resolves without any lookup)."""
    return crypto.did_key_verification_method(identity["did"])


# --- signed offer (spec §4) ---------------------------------------------------

def offer_payload(*, resource_url: str, scheme: str, network: str, asset: str,
                  pay_to: str, amount: str,
                  valid_until: Optional[int] = None) -> dict[str, Any]:
    """The canonical offer payload (spec §4.2). Field ORDER is irrelevant to
    JWS (bytes are signed as serialized), but keys/values are normative."""
    return {
        "version": PAYLOAD_VERSION,
        "resourceUrl": resource_url,
        "scheme": scheme,
        "network": network,
        "asset": asset,
        "payTo": pay_to,
        "amount": amount,
        "validUntil": int(valid_until if valid_until is not None
                          else time.time() + OFFER_TTL_SECONDS),
    }


def signed_offer(identity: dict[str, Any], payload: dict[str, Any],
                 accept_index: int = 0) -> dict[str, Any]:
    """A JWS signed offer object (spec §3.1: format/signature; payload is
    OMITTED for JWS — it lives inside the compact string)."""
    return {
        "format": "jws",
        "acceptIndex": accept_index,
        "signature": jws_sign(payload, identity["private_key"],
                              kid_for_identity(identity)),
    }


def offer_receipt_challenge_extension(identity: dict[str, Any],
                                      offers: list[dict[str, Any]]) -> dict[str, Any]:
    """extensions["offer-receipt"] value for a 402 challenge (spec §6.3)."""
    return {
        "info": {"offers": offers},
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "offers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "format": {"type": "string", "const": "jws"},
                            "acceptIndex": {"type": "integer"},
                            "signature": {
                                "type": "string",
                                "description": ("JWS compact serialization "
                                                "containing the offer payload"),
                            },
                        },
                        "required": ["format", "signature"],
                    },
                }
            },
            "required": ["offers"],
        },
    }


# --- signed receipt (spec §5) ---------------------------------------------------

def receipt_payload(*, network: str, resource_url: str, payer: str,
                    transaction: str = "",
                    issued_at: Optional[int] = None) -> dict[str, Any]:
    """The canonical receipt payload (spec §5.2). The Guild always includes
    the transaction hash: verifiability over privacy is the whole point of a
    trust ledger's receipts."""
    return {
        "version": PAYLOAD_VERSION,
        "network": network,
        "resourceUrl": resource_url,
        "payer": payer,
        "issuedAt": int(issued_at if issued_at is not None else time.time()),
        "transaction": transaction or "",
    }


def signed_receipt(identity: dict[str, Any],
                   payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": "jws",
        "signature": jws_sign(payload, identity["private_key"],
                              kid_for_identity(identity)),
    }


def offer_receipt_settle_extension(receipt: dict[str, Any]) -> dict[str, Any]:
    """extensions["offer-receipt"] value for a SettleResponse (spec §6.7)."""
    return {
        "info": {"receipt": receipt},
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "receipt": {
                    "type": "object",
                    "properties": {
                        "format": {"type": "string", "const": "jws"},
                        "signature": {
                            "type": "string",
                            "description": ("JWS compact serialization "
                                            "containing the receipt payload"),
                        },
                    },
                    "required": ["format", "signature"],
                }
            },
            "required": ["receipt"],
        },
    }


# --- Agent Guild evidence attachment (namespaced SIBLING extension) -----------

def evidence_extension(identity: dict[str, Any], *, resource_url: str,
                       request_hash: str, response_sha256: str,
                       transaction: str, payer: str,
                       payment_identifier_sha256: Optional[str],
                       checkpoint: Optional[dict[str, Any]]) -> dict[str, Any]:
    """extensions["io.agent-guild/evidence"]: the Guild-specific evidence the
    standard receipt intentionally does not carry — the sha256 of the exact
    response bytes the payer received, the request hash the payment was bound
    to, and the AGI-1 checkpoint pin current at issue time. Signed as its own
    JWS by the same service identity; the standard `offer-receipt` fields are
    never touched."""
    payload: dict[str, Any] = {
        "version": 1,
        "resourceUrl": resource_url,
        "requestHash": request_hash,
        "responseSha256": response_sha256,
        "transaction": transaction or "",
        "payer": payer,
        "issuedAt": int(time.time()),
        "paymentIdentifierSha256": payment_identifier_sha256 or "",
        "agi1Checkpoint": checkpoint or None,
    }
    return {
        "info": {
            **payload,
            "jws": jws_sign(payload, identity["private_key"],
                            kid_for_identity(identity)),
        },
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "responseSha256": {"type": "string"},
                "requestHash": {"type": "string"},
                "jws": {"type": "string"},
            },
            "required": ["responseSha256", "requestHash", "jws"],
        },
    }


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
