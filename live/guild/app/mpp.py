"""MPP (Machine Payments Protocol) → x402 conversion layer.

Post-adversarial-review architecture (2026-08-14): Agent Guild does NOT grow
a second settlement implementation. An authenticated MPP ``evm``/``charge``
credential carrying an EIP-3009 authorization is CONVERTED into the official
x402 v2 ``PaymentPayload`` and settled through the EXISTING
``payments.authorize → settle_x402`` path — same facilitator, same durable
payment-identifier crash recovery, same independent mainnet confirmation,
same receipts and evidence. mppx (the official TS client, 0.8.x) settles
native Payment-auth evm/charge through an x402 facilitator the same way.

Challenge binding (fixes the failed dark-launch design):
  * The challenge id is HMAC-SHA256 over the pympp/spec seven-slot input
    ``realm|method|intent|request_b64|expires|digest|opaque`` — and the
    ``opaque`` slot carries ``{route, request_hash}`` for the EXACT
    ``PaidRequest`` being priced, so route, canonical query and price are
    all inside the MAC. Nothing about a challenge can be transplanted.
  * The client's EIP-3009 ``nonce`` MUST equal
    ``0x + keccak256(challenge.id + realm)`` — the on-chain single-use
    nonce is cryptographically bound to THIS challenge, so no previously
    signed or replayed authorization can satisfy a new challenge, and the
    chain itself enforces single use of the bound nonce.
  * Amounts are EXACT (check_binding equality), never >=.
  * Expiry parsing is UTC-correct (``calendar.timegm``, never
    ``time.mktime``).

Gating: MPP is live whenever the already-hardened x402 rail is live. Operators
may set ``GUILD_MPP_ENABLED=0`` as a kill switch.  Challenge MAC keys are
domain-separated from the Guild's existing persistent signing identity, so
launching the HTTP compatibility surface neither creates nor transports a new
deployment credential.  Tests may override the derived key with
``GUILD_MPP_SECRET``.  The same readiness predicate drives advertisement and
acceptance, so a challenge is never advertised while unacceptable.

Revenue: a converted settlement IS an x402 settlement (mode "x402"), subject
to the unchanged three-condition revenue rule; the wire protocol is carried
separately as ``protocol="mpp_evm"`` on the settlement record and events.
No new externality claim is introduced.
"""
from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from eth_hash.auto import keccak

from . import x402
from .x402 import PaymentPayload

WWW_AUTHENTICATE = "WWW-Authenticate"
PAYMENT_RECEIPT_HEADER = "Payment-Receipt"
PROBLEM_BASE = "https://paymentauth.org/problems/"

_METHOD = "evm"
_INTENT = "charge"
_KEY_CONTEXT = b"agent-guild/mpp-http-challenge/v1"
_USDC_DECIMALS = 6


def enabled() -> bool:
    flag = (os.environ.get("GUILD_MPP_ENABLED") or "1").strip()
    if flag != "1" or not x402.enabled():
        return False
    try:
        _secret()
        return True
    except MppError:
        return False


def _secret() -> bytes:
    override = os.environ.get("GUILD_MPP_SECRET")
    if override is not None:
        raw = override.encode()
        if len(raw) < 32:
            raise MppError("invalid-challenge",
                           "MPP misconfigured: GUILD_MPP_SECRET must be >=32 "
                           "bytes", status=500)
        return raw

    # No new production secret: the issuer key is already durable and private.
    # HMAC domain separation means this key cannot be used as an Ed25519 key,
    # and challenge MACs reveal nothing about the underlying issuer material.
    try:
        from .state import store
        issuer_seed = bytes.fromhex(store.guild_identity()["private_key"])
    except Exception as exc:
        raise MppError("invalid-challenge",
                       "MPP challenge key is unavailable", status=500) from exc
    return hmac.new(issuer_seed, _KEY_CONTEXT, hashlib.sha256).digest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class MppError(Exception):
    """RFC 9457 problem, using the spec's registered type slugs."""

    def __init__(self, slug: str, detail: str = "", status: int = 402):
        super().__init__(detail or slug)
        self.slug = slug
        self.status = status

    @property
    def problem(self) -> dict:
        return {"type": PROBLEM_BASE + self.slug, "status": self.status,
                "title": self.slug.replace("-", " "), "detail": str(self)}


# ---------------------------------------------------------------------------
# Challenge — pympp/spec-exact seven-slot HMAC id
# ---------------------------------------------------------------------------
def _challenge_id(secret: bytes, realm: str, request_b64: str, expires: str,
                  digest: str, opaque_b64: str) -> str:
    msg = "|".join([realm, _METHOD, _INTENT, request_b64, expires, digest,
                    opaque_b64]).encode()
    return _b64url(hmac.new(secret, msg, hashlib.sha256).digest())


@dataclass(frozen=True)
class Challenge:
    id: str
    realm: str
    request_b64: str
    expires: str
    digest: str
    opaque_b64: str

    def header_value(self) -> str:
        parts = [f'id="{self.id}"', f'realm="{self.realm}"',
                 f'method="{_METHOD}"', f'intent="{_INTENT}"',
                 f'request="{self.request_b64}"',
                 f'expires="{self.expires}"']
        if self.digest:
            parts.append(f'digest="{self.digest}"')
        parts.append(f'opaque="{self.opaque_b64}"')
        return "Payment " + ", ".join(parts)


def _quote_for(preq, cost: int) -> dict:
    """The MPP request blob, derived from the SAME source the x402 challenge
    quotes (x402.requirements) so the two protocols can never disagree."""
    offered = x402.requirements(cost)
    return {
        "amount": offered.amount,                       # exact atomic units
        "currency": offered.asset,                      # token address
        "recipient": offered.pay_to,
        "methodDetails": {"chainId": int(str(offered.network).split(":")[-1]),
                          "credentialTypes": ["authorization"],
                          "decimals": _USDC_DECIMALS},
    }


def _opaque_for(preq) -> str:
    """Route + canonical request binding, INSIDE the HMAC."""
    return _b64url(json.dumps({
        "route": f"{preq.method} {preq.path}",
        "request_hash": preq.request_hash,
    }, sort_keys=True, separators=(",", ":")).encode())


def mint_challenge(preq, cost: int, ttl_s: int = 300) -> Challenge:
    request_b64 = _b64url(json.dumps(_quote_for(preq, cost), sort_keys=True,
                                     separators=(",", ":")).encode())
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(time.time() + ttl_s))
    realm = x402.public_host().split("//", 1)[-1]
    opaque_b64 = _opaque_for(preq)
    cid = _challenge_id(_secret(), realm, request_b64, expires, "", opaque_b64)
    return Challenge(id=cid, realm=realm, request_b64=request_b64,
                     expires=expires, digest="", opaque_b64=opaque_b64)


def expected_nonce(challenge_id: str, realm: str) -> str:
    """EIP-3009 nonce bound to THIS challenge: keccak256(id + realm)."""
    return "0x" + keccak((challenge_id + realm).encode()).hex()


# ---------------------------------------------------------------------------
# Credential → official x402 PaymentPayload
# ---------------------------------------------------------------------------
def is_mpp_authorization(header_value: str) -> bool:
    return (header_value or "").strip().lower().startswith("payment ")


def precedence_check(payment_signature: str, authorization: str) -> None:
    """ONE PROTOCOL PER REQUEST, decided before any decode: a request
    carrying both an x402 PAYMENT-SIGNATURE and an MPP Payment credential is
    rejected outright — ambiguous intent must never risk two settlements."""
    if payment_signature and is_mpp_authorization(authorization):
        raise MppError("malformed-credential",
                       "both x402 PAYMENT-SIGNATURE and MPP Authorization: "
                       "Payment present — send exactly one payment protocol",
                       status=400)


def _parse_credential(authorization: str) -> dict:
    val = (authorization or "").strip()
    if not val.lower().startswith("payment "):
        raise MppError("malformed-credential", "not the Payment scheme")
    try:
        cred = json.loads(_b64url_decode(val[8:].strip()))
    except Exception:
        raise MppError("malformed-credential",
                       "credential is not base64url JSON")
    if not isinstance(cred, dict) or not isinstance(cred.get("challenge"),
                                                    dict):
        raise MppError("malformed-credential", "missing challenge echo")
    return cred


def credential_to_payment(authorization: str, preq,
                          cost: int) -> Tuple[PaymentPayload, str]:
    """Authenticate the MPP credential against THIS request's challenge and
    convert it into the official x402 v2 PaymentPayload.

    Everything downstream — exact resource/amount/recipient/window binding,
    replay reservation, facilitator verify+settle, durable crash recovery,
    independent mainnet confirmation, receipt/evidence finalisation — is the
    EXISTING settle_x402 path. This function only authenticates the MPP wire
    envelope and refuses anything that is not bound to this exact challenge.

    Returns (payload, payer_source). Raises MppError.
    """
    cred = _parse_credential(authorization)
    ch = cred["challenge"]
    realm = str(ch.get("realm", ""))
    request_b64 = str(ch.get("request", ""))
    expires = str(ch.get("expires", ""))
    digest = str(ch.get("digest", "") or "")
    opaque_b64 = str(ch.get("opaque", "") or "")

    # 1. Authenticity: recompute the HMAC id from the echoed slots. Any
    #    tampering (price, route, expiry, realm) breaks the id.
    expect = _challenge_id(_secret(), realm, request_b64, expires, digest,
                           opaque_b64)
    if not hmac.compare_digest(expect, str(ch.get("id", ""))):
        raise MppError("invalid-challenge", "challenge id does not verify")

    # 2. Expiry — UTC-correct (RFC 3339 Zulu; never local time).
    try:
        exp = calendar.timegm(time.strptime(expires, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        raise MppError("invalid-challenge", "unparseable expires")
    if time.time() > exp:
        raise MppError("payment-expired", "challenge expired")

    # 3. The challenge must be for THIS route and THIS canonical request.
    try:
        bound = json.loads(_b64url_decode(opaque_b64))
    except Exception:
        raise MppError("invalid-challenge", "opaque binding unreadable")
    if bound.get("route") != f"{preq.method} {preq.path}" or \
            bound.get("request_hash") != preq.request_hash:
        raise MppError("invalid-challenge",
                       "challenge is bound to a different request")

    # 4. ... and must quote EXACTLY what this request costs now.
    try:
        quoted = json.loads(_b64url_decode(request_b64))
    except Exception:
        raise MppError("invalid-challenge", "request blob unreadable")
    if quoted != _quote_for(preq, cost):
        raise MppError("invalid-challenge",
                       "challenge quote does not match the current price for "
                       "this request")

    # 4b. The echoed envelope MUST be ours: canonical realm, evm, charge
    #     (official pympp server rejects realm/method/intent mismatch too).
    if realm != x402.public_host().split("//", 1)[-1]:
        raise MppError("invalid-challenge", "realm is not this server")
    if ch.get("method") != _METHOD or ch.get("intent") != _INTENT:
        raise MppError("invalid-challenge",
                       "challenge is not an evm/charge challenge")

    # 5. OFFICIAL flat EVM charge payload (mppx src/evm/Types.ts
    #    AuthorizationPayloadSchema): from, nonce, signature, to,
    #    type='authorization', validAfter, validBefore, value DIRECTLY in
    #    credential.payload — with the challenge-bound nonce
    #    (mppx Types.challengeHash = keccak256(id + realm)).
    p = cred.get("payload") or {}
    required = {"from", "nonce", "signature", "to", "type", "validAfter",
                "validBefore", "value"}
    if not isinstance(p, dict) or p.get("type") != "authorization" or \
            not required <= set(p.keys()):
        raise MppError("verification-failed",
                       "payload is not the official flat EVM authorization "
                       "shape")
    sig = p["signature"]
    auth3009 = {k: p[k] for k in ("from", "to", "value", "validAfter",
                                  "validBefore", "nonce")}
    want_nonce = expected_nonce(str(ch["id"]), realm)
    if str(p.get("nonce", "")).lower() != want_nonce.lower():
        raise MppError("verification-failed",
                       "EIP-3009 nonce is not keccak256(challenge.id + "
                       "realm) — authorization is not bound to this "
                       "challenge")
    # 5b. Payer identity binding: when a source DID is present it MUST be
    #     did:pkh:eip155:<chainId>:<address> matching payload.from and the
    #     quoted chain (official mppx server semantics).
    source = str(cred.get("source") or "")
    if source:
        m2 = source.split(":")
        chain = json.loads(_b64url_decode(request_b64))["methodDetails"][
            "chainId"]
        if len(m2) != 5 or m2[0] != "did" or m2[1] != "pkh" or \
                m2[2] != "eip155" or m2[3] != str(chain) or \
                m2[4].lower() != str(p["from"]).lower():
            raise MppError("verification-failed",
                           "source DID does not match payload.from on the "
                           "quoted chain")

    # 6. Convert. The accepted requirements and resource echo are OURS
    #    (derived server-side), so check_binding's exact comparisons run
    #    against the same objects the x402 path itself quotes.
    converted = PaymentPayload.model_validate({
        "x402Version": 2,
        "payload": {"signature": sig, "authorization": auth3009},
        "accepted": json.loads(x402.requirements(cost).model_dump_json(
            by_alias=True)),
        "resource": {"url": preq.resource_url},
    })
    return converted, str(cred.get("source") or "")


def receipt_header_value(record: dict) -> str:
    """Payment-Receipt for a CONFIRMED settlement (success-only, per spec)."""
    if record.get("confirmed") is not True or not record.get("transaction"):
        raise MppError("verification-failed",
                       "a Payment-Receipt requires confirmed settlement",
                       status=500)
    return _b64url(json.dumps({
        "status": "success",
        "method": _METHOD,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "reference": str(record.get("transaction") or ""),
    }, sort_keys=True).encode())
