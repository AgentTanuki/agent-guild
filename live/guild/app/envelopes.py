"""Paid, caller-bound machine communication envelopes.

The Guild's trust products historically answered questions *about* an agent.
This module supplies the more general primitive autonomous systems repeatedly
need while talking to one another: a compact, non-repudiable record of exactly
which digest a machine sent, to whom, for what purpose and until when.

The Guild does **not** sign the truth of the message.  It signs a much narrower
and checkable statement: a valid ``agent-guild/caller-proof/v1`` authenticated
the sender DID for this issuance request, and the Guild observed and sealed the
canonical envelope at ``issued_at``.  Payloads stay with the participants; only
their SHA-256 commitment reaches this service.

Issuance fails closed.  A caller is never charged for an unsigned or malformed
artefact.  Verification is free and can also be implemented offline from the
published did:key.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from . import callerproof
from .crypto import canonicalize_jcs, sign_jcs

PROTOCOL = "agent-guild/machine-envelope/v1"
TYPE = "AgentGuildMachineEnvelope"
DEFAULT_TTL_S = 3600
MAX_TTL_S = 7 * 24 * 3600
MAX_CONTEXT_BYTES = 4096

KINDS = frozenset({
    "message", "intent", "offer", "acceptance", "delegation",
    "authorization", "delivery", "receipt", "revocation",
})

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+#-]{0,127}$")


class EnvelopeIssuanceRefused(ValueError):
    """The requested artefact is invalid or cannot be signed; do not bill."""

    code = "machine_envelope_issuance_refused"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bounded_string(value: Any, field: str, *, minimum: int = 1,
                    maximum: int = 512) -> str:
    out = str(value or "").strip()
    if not (minimum <= len(out) <= maximum):
        raise EnvelopeIssuanceRefused(
            f"{field} must be {minimum}..{maximum} characters")
    return out


def _optional_string(value: Any, field: str, maximum: int = 512
                     ) -> str | None:
    if value is None or value == "":
        return None
    return _bounded_string(value, field, maximum=maximum)


def _normalise_value(raw: Any) -> dict[str, str] | None:
    """Validate optional economic terms without claiming settlement.

    Amount is a decimal *string*, never a binary float.  Network and asset are
    identifiers only; their presence says what the sender intended, not that a
    transfer happened.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EnvelopeIssuanceRefused("value must be an object")
    allowed = {"amount", "asset", "network", "payee"}
    unknown = set(raw) - allowed
    if unknown:
        raise EnvelopeIssuanceRefused(
            "value contains unsupported fields: " + ", ".join(sorted(unknown)))
    amount = _bounded_string(raw.get("amount"), "value.amount", maximum=80)
    try:
        parsed = Decimal(amount)
    except InvalidOperation as exc:
        raise EnvelopeIssuanceRefused(
            "value.amount must be a finite decimal string") from exc
    if not parsed.is_finite() or parsed < 0:
        raise EnvelopeIssuanceRefused(
            "value.amount must be a non-negative finite decimal string")
    asset = _bounded_string(raw.get("asset"), "value.asset", maximum=128)
    network = _bounded_string(raw.get("network"), "value.network", maximum=128)
    if not _SAFE_TOKEN_RE.fullmatch(asset) or not _SAFE_TOKEN_RE.fullmatch(network):
        raise EnvelopeIssuanceRefused(
            "value.asset and value.network must be machine identifiers")
    out = {"amount": amount, "asset": asset, "network": network}
    payee = _optional_string(raw.get("payee"), "value.payee")
    if payee:
        out["payee"] = payee
    return out


def normalise_request(body: Any) -> dict[str, Any]:
    """Return the canonical, privacy-preserving issuance request."""
    if not isinstance(body, dict):
        raise EnvelopeIssuanceRefused("body must be a JSON object")
    kind = str(body.get("kind") or "message").strip().lower()
    if kind not in KINDS:
        raise EnvelopeIssuanceRefused(
            "kind must be one of: " + ", ".join(sorted(KINDS)))
    digest = str(body.get("payload_sha256") or "").strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise EnvelopeIssuanceRefused(
            "payload_sha256 must be the 64-character hex SHA-256 of the exact "
            "message bytes; send the digest, not the confidential payload")
    nonce = _bounded_string(body.get("nonce"), "nonce", minimum=8, maximum=128)
    recipient = _bounded_string(body.get("recipient"), "recipient")
    ttl = body.get("ttl_seconds", DEFAULT_TTL_S)
    try:
        ttl = int(ttl)
    except (TypeError, ValueError) as exc:
        raise EnvelopeIssuanceRefused("ttl_seconds must be an integer") from exc
    if not (60 <= ttl <= MAX_TTL_S):
        raise EnvelopeIssuanceRefused(
            f"ttl_seconds must be between 60 and {MAX_TTL_S}")

    context = body.get("context")
    if context is not None and not isinstance(context, dict):
        raise EnvelopeIssuanceRefused("context must be an object")
    context = context or {}
    try:
        context_bytes = canonicalize_jcs(context).encode("utf-8")
    except Exception as exc:  # non-finite floats / non-JSON values
        raise EnvelopeIssuanceRefused("context must be canonical JSON") from exc
    if len(context_bytes) > MAX_CONTEXT_BYTES:
        raise EnvelopeIssuanceRefused(
            f"context exceeds {MAX_CONTEXT_BYTES} canonical bytes")

    out: dict[str, Any] = {
        "kind": kind,
        "recipient": recipient,
        "payload_sha256": digest,
        "nonce": nonce,
        "ttl_seconds": ttl,
    }
    for name, limit in (("payload_media_type", 128), ("resource", 1024),
                        ("reply_to", 512), ("constraints_sha256", 64)):
        value = _optional_string(body.get(name), name, maximum=limit)
        if value is not None:
            if name == "constraints_sha256":
                value = value.lower()
                if not _SHA256_RE.fullmatch(value):
                    raise EnvelopeIssuanceRefused(
                        "constraints_sha256 must be 64 hex characters")
            out[name] = value
    value = _normalise_value(body.get("value"))
    if value is not None:
        out["value"] = value
    if context:
        out["context"] = context
    return out


def request_sha256(body: Any, sender_did: str) -> str:
    """Opaque exact-request binding used by the payment challenge."""
    normalized = normalise_request(body)
    bound = {"protocol": PROTOCOL, "sender": sender_did,
             "request": normalized}
    return hashlib.sha256(
        canonicalize_jcs(bound).encode("utf-8")).hexdigest()


def issue(store: Any, body: Any, *, sender_did: str,
          caller_proof_verified: bool) -> dict[str, Any]:
    """Issue one caller-authenticated envelope.  Fails closed."""
    authentication = callerproof.authentication_protocol_for_did(sender_did)
    if not caller_proof_verified or authentication is None:
        raise EnvelopeIssuanceRefused(
            "a valid did:key or Base-wallet caller proof is required; AG "
            "will not sell an anonymous rubber stamp")
    req = normalise_request(body)
    gid = store.guild_identity()
    if not gid.get("did") or not gid.get("private_key"):
        raise EnvelopeIssuanceRefused(
            "the Guild signing identity is unavailable")

    issued = _now()
    valid_until = issued + timedelta(seconds=req.pop("ttl_seconds"))
    commitment = {"protocol": PROTOCOL, "sender": sender_did,
                  "message": req}
    commitment_sha = hashlib.sha256(
        canonicalize_jcs(commitment).encode("utf-8")).hexdigest()
    body_to_sign: dict[str, Any] = {
        "type": TYPE,
        "version": 1,
        "id": "urn:sha256:" + commitment_sha,
        "protocol": PROTOCOL,
        "issuer": gid["did"],
        "issued_at": issued.isoformat(),
        "valid_until": valid_until.isoformat(),
        "sender": {
            "did": sender_did,
            "authentication": authentication,
            "caller_proof_verified": True,
        },
        "message": req,
        "attestation_scope": {
            "attested": (
                "Agent Guild received an issuance request authenticated by "
                "the sender DID and sealed this exact canonical envelope "
                "at issued_at."),
            "not_attested": [
                "truth or legality of the payload",
                "recipient receipt, acceptance or countersignature",
                "payment or settlement of any declared economic value",
                "future behaviour of either party",
            ],
            "privacy": (
                "The Guild received only a SHA-256 commitment to the payload, "
                "not the payload itself."),
        },
        "verification": {
            "suite": "eddsa-jcs-2022",
            "issuer_did_document": "/.well-known/agent-guild-did.json",
            "online": "POST /envelopes/verify (free)",
            "offline": (
                "Remove proof and envelope_sha256, JCS-canonicalize the "
                "remaining object, and verify the hex Ed25519 proof against "
                "issuer did:key. Then enforce valid_until and your own policy."),
        },
    }
    try:
        proof = sign_jcs(body_to_sign, gid["private_key"])
    except Exception as exc:  # noqa: BLE001
        raise EnvelopeIssuanceRefused(
            f"signing failed: {type(exc).__name__}") from exc
    if not proof:
        raise EnvelopeIssuanceRefused("signing produced no proof")
    envelope = {**body_to_sign, "proof": proof}
    envelope["envelope_sha256"] = hashlib.sha256(
        canonicalize_jcs(envelope).encode("utf-8")).hexdigest()
    return envelope


def verify(store: Any, envelope: Any) -> dict[str, Any]:
    """Verify an issued envelope.  Free; signature verification is offline."""
    from .crypto import public_key_from_did, verify_jcs

    if not isinstance(envelope, dict) or not isinstance(envelope.get("proof"), str):
        return {"valid": False, "reason": "not a machine envelope (no proof)"}
    if envelope.get("type") != TYPE or envelope.get("protocol") != PROTOCOL:
        return {"valid": False, "reason": "unsupported envelope type/protocol"}
    issuer = str(envelope.get("issuer") or "")
    try:
        known = list(store.guild_did_history())
    except Exception:  # noqa: BLE001
        known = []
    if issuer not in known:
        return {"valid": False, "reason": "issuer is not a Guild key",
                "issuer": issuer}

    claimed_digest = str(envelope.get("envelope_sha256") or "")
    without_digest = {k: v for k, v in envelope.items()
                      if k != "envelope_sha256"}
    actual_digest = hashlib.sha256(
        canonicalize_jcs(without_digest).encode("utf-8")).hexdigest()
    digest_valid = bool(claimed_digest) and claimed_digest == actual_digest
    signed_body = {k: v for k, v in envelope.items()
                   if k not in ("proof", "envelope_sha256")}
    try:
        signature_valid = verify_jcs(
            signed_body, envelope["proof"], public_key_from_did(issuer))
    except Exception:  # noqa: BLE001
        signature_valid = False
    try:
        expired = _now() > datetime.fromisoformat(str(envelope["valid_until"]))
    except (KeyError, TypeError, ValueError):
        expired = True

    sender = envelope.get("sender") or {}
    message = envelope.get("message") or {}
    return {
        "valid": bool(signature_valid and digest_valid and not expired),
        "signature_valid": bool(signature_valid),
        "digest_valid": digest_valid,
        "expired": expired,
        "issuer": issuer,
        "sender_did": sender.get("did"),
        "recipient": message.get("recipient"),
        "kind": message.get("kind"),
        "payload_sha256": message.get("payload_sha256"),
        "issued_at": envelope.get("issued_at"),
        "valid_until": envelope.get("valid_until"),
        "note": (
            "Valid means integrity, Guild provenance, authenticated sender at "
            "issuance and unexpired lifetime. It does not make the payload true."),
    }


def schema_document(base: str = "") -> dict[str, Any]:
    root = base.rstrip("/")
    return {
        "protocol": PROTOCOL,
        "purpose": (
            "paid issuance of privacy-preserving, sender-authenticated machine "
            "communication commitments; verification is free and offline-capable"),
        "issue": {
            "method": "POST", "path": "/envelopes/issue",
            # Keep the original scalar stable; options is the additive field.
            "caller_authentication": callerproof.PROTOCOL,
            "caller_authentication_options": list(
                callerproof.SUPPORTED_PROTOCOLS),
            "caller_proof_header": callerproof.HTTP_HEADER,
            "payment": "x402 USDC on Base mainnet or sandbox credits",
            "body": {
                "kind": sorted(KINDS),
                "recipient": "DID, URL, wallet, agent id or other identifier",
                "payload_sha256": "sha256 hex of the exact payload bytes",
                "nonce": "caller-chosen unique message id, 8..128 chars",
                "ttl_seconds": f"60..{MAX_TTL_S}; default {DEFAULT_TTL_S}",
                "optional": ["payload_media_type", "resource", "reply_to",
                             "constraints_sha256", "value", "context"],
            },
        },
        "verify": {"method": "POST", "path": "/envelopes/verify",
                   "price": "free", "offline_capable": True},
        "client": {
            "language": "javascript/typescript (node)",
            "source": "/sdk/agentguild_envelope_client.mjs",
            "function": "createEvmMachineEnvelopeClient({evmSigner}).issue(...)",
            "payment_dependencies": ["@x402/fetch", "@x402/evm"],
            "note": ("one function invocation hashes private payload bytes, "
                     "uses the same caller-owned Base EOA for exact-body "
                     "authentication and x402 payment, and verifies the "
                     "returned Guild signature offline; a separate did:key "
                     "signer remains supported"),
        },
        "issuer": ((root + "/.well-known/agent-guild-did.json") if root
                   else "/.well-known/agent-guild-did.json"),
        "honesty": (
            "The Guild signs observation, exact digest, sender authentication "
            "and time. It does not endorse message truth or claim settlement."),
    }
