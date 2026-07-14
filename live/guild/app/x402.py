"""x402 v2 machine payment rail (https://www.x402.org — protocol version 2).

Real-money rail for machine-to-machine settlement: no card, no browser, no
human checkout. A paid resource answers HTTP 402 with a base64
`PAYMENT-REQUIRED` header (and the same JSON in the body, alongside the
sandbox-credit instructions); the client retries with a `PAYMENT-SIGNATURE`
header carrying a signed v2 PaymentPayload; the server verifies and settles
through a FACILITATOR and returns the settlement in a `PAYMENT-RESPONSE`
header. Types, header codecs and the facilitator client come from the
official maintained SDK (`x402` on PyPI, pinned in requirements.txt); the
Guild adds strict server-side BINDING and REPLAY guards on top, because the
facilitator cannot know which resource/price/recipient THIS server quoted.

Spec: x402 specs/x402-specification-v2.md + specs/transports-v2/http.md
(x402Version 2, CAIP-2 networks, PAYMENT-REQUIRED / PAYMENT-SIGNATURE /
PAYMENT-RESPONSE headers).

Honesty notes, load-bearing:
  * Credits remain available and are EXPLICITLY a sandbox settlement unit
    (`credits_sandbox`) — not money, labelled as such wherever they appear.
  * Default network is eip155:84532 (Base Sepolia — TESTNET, value-less)
    until a funded mainnet treasury exists. Every 402 discloses the network.
  * REAL revenue is counted only from successful settlements on a MAINNET
    network with a transaction hash (store.revenue → real_settlement).
    Testnet/mocked settlements are recorded separately and never counted.
  * The legacy v1 protocol (X-PAYMENT header, x402Version 1, non-CAIP
    network names) is still accepted TEMPORARILY — the official SDK keeps
    v1 legacy support — but it is labelled deprecated and passes through the
    SAME binding/replay guards as v2 (tests assert it cannot weaken them).

Env:
  GUILD_X402_ENABLED       "1" to advertise/accept x402 (default off until a
                           payTo address is configured)
  GUILD_X402_PAY_TO        the Guild treasury address (EVM 0x…)
  GUILD_X402_NETWORK       CAIP-2, default "eip155:84532" (Base Sepolia)
  GUILD_X402_ASSET         ERC-20 contract (default: USDC on Base Sepolia)
  GUILD_X402_FACILITATOR   default "https://x402.org/facilitator"
  GUILD_PUBLIC_HOST        canonical public origin for resource URLs
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from typing import Any, Optional

from x402.http import (
    PAYMENT_REQUIRED_HEADER,     # "PAYMENT-REQUIRED"   (402 → client)
    PAYMENT_RESPONSE_HEADER,     # "PAYMENT-RESPONSE"   (settlement → client)
    PAYMENT_SIGNATURE_HEADER,    # "PAYMENT-SIGNATURE"  (client → server)
    X_PAYMENT_HEADER,            # "X-PAYMENT"          (v1 legacy)
    FacilitatorConfig,
    HTTPFacilitatorClientSync,
)
from x402.http.utils import (
    decode_payment_signature_header,
    encode_payment_required_header,
    encode_payment_response_header,
)
from x402.schemas import (
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    ResourceInfo,
    SettleResponse,
)

X402_VERSION = 2
DEFAULT_NETWORK = "eip155:84532"            # Base Sepolia (CAIP-2)
# Circle USDC on Base Sepolia
DEFAULT_ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
DEFAULT_FACILITATOR = "https://x402.org/facilitator"
DEFAULT_HOST = "https://agent-guild-5d5r.onrender.com"

# Networks whose successful settlement is REAL value. Everything else
# (testnets, local fakes) is value-less and must never count as revenue.
MAINNET_NETWORKS = frozenset({
    "eip155:8453",      # Base mainnet
    "eip155:43114",     # Avalanche mainnet
    "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",  # Solana mainnet
})

# v1 legacy network names → CAIP-2 (for the deprecated X-PAYMENT path only).
V1_NETWORK_TO_CAIP2 = {
    "base-sepolia": "eip155:84532",
    "base": "eip155:8453",
    "avalanche-fuji": "eip155:43113",
    "avalanche": "eip155:43114",
}

# 1 credit (sandbox) is priced at $0.001 (see billing.CREDIT_USD); USDC has 6
# decimals, so 1 credit == 1000 atomic USDC units on the real rail.
ATOMIC_PER_CREDIT = 1000

# Canonical resource URL path per priced capability (billing.PRICING key).
# The quote, the client's echoed `resource`, and the settlement record are all
# bound to this canonical URL — path templates are literal identifiers here.
RESOURCE_PATHS = {
    "best_agent": "/check",
    "reputation": "/agents/{id}/reputation",
    "evidence": "/agents/{id}/evidence",
    "risk_score": "/agents/{id}/risk-score",
    "fraud_check": "/agents/{id}/flags",
}
# All priced reads are GETs; the method is part of the binding.
RESOURCE_METHOD = "GET"


def enabled() -> bool:
    return (os.environ.get("GUILD_X402_ENABLED", "0") == "1"
            and bool(pay_to()))


def pay_to() -> str:
    return os.environ.get("GUILD_X402_PAY_TO", "").strip()


def network() -> str:
    net = os.environ.get("GUILD_X402_NETWORK", DEFAULT_NETWORK)
    # accept a legacy v1 name in the env for operator convenience, but the
    # protocol surface is always CAIP-2
    return V1_NETWORK_TO_CAIP2.get(net, net)


def is_mainnet(net: str) -> bool:
    return net in MAINNET_NETWORKS


def asset() -> str:
    return os.environ.get("GUILD_X402_ASSET", DEFAULT_ASSET)


def facilitator_url() -> str:
    return os.environ.get("GUILD_X402_FACILITATOR", DEFAULT_FACILITATOR).rstrip("/")


def public_host() -> str:
    return os.environ.get("GUILD_PUBLIC_HOST", DEFAULT_HOST).rstrip("/")


def resource_url(endpoint: str) -> str:
    return public_host() + RESOURCE_PATHS.get(endpoint, f"/x402/resources/{endpoint}")


def requirements(endpoint: str, credits_cost: int) -> PaymentRequirements:
    """The v2 payment requirements the Guild quotes for one capability."""
    return PaymentRequirements(
        scheme="exact",
        network=network(),
        amount=str(credits_cost * ATOMIC_PER_CREDIT),
        asset=asset(),
        pay_to=pay_to(),
        max_timeout_seconds=300,
        extra={"name": "USDC", "version": "2"},
    )


def resource_info(endpoint: str) -> ResourceInfo:
    return ResourceInfo(
        url=resource_url(endpoint),
        description=f"Agent Guild paid read: {endpoint}",
        mime_type="application/json",
    )


# Bazaar discovery extension (x402 specs/extensions/bazaar.md): machine-
# readable endpoint specifications inside the 402 challenge, so facilitator
# catalogues can index the Guild's paid trust operations without a human.
_BAZAAR_QUERY = {
    "best_agent": {"capability": "code-review"},
}
_BAZAAR_OUTPUT = {
    "best_agent": {"verdict": "hire", "best": {"agent_id": "…", "score": 0.93}},
    "reputation": {"score": 0.9, "confidence": 0.8},
    "evidence": {"attestations": [], "receipts": []},
    "risk_score": {"risk": 12, "recommendation": "hire"},
    "fraud_check": {"suspicion": 0.02, "flags": []},
}


def bazaar_extension(endpoint: str) -> dict[str, Any]:
    info: dict[str, Any] = {
        "input": {"type": "http", "method": RESOURCE_METHOD,
                  **({"queryParams": _BAZAAR_QUERY[endpoint]}
                     if endpoint in _BAZAAR_QUERY else {})},
        "output": {"type": "json",
                   "example": _BAZAAR_OUTPUT.get(endpoint, {})},
    }
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "input": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "const": "http"},
                    "method": {"type": "string", "enum": [RESOURCE_METHOD]},
                    "queryParams": {"type": "object",
                                    "additionalProperties": {"type": "string"}},
                },
                "required": ["type", "method"],
            },
            "output": {
                "type": "object",
                "properties": {"type": {"type": "string"},
                               "example": {"type": "object"}},
                "required": ["type"],
            },
        },
        "required": ["input"],
    }
    return {"info": info, "schema": schema}


def payment_required_model(endpoint: str, credits_cost: int) -> PaymentRequired:
    return PaymentRequired(
        x402_version=X402_VERSION,
        error=f"{PAYMENT_SIGNATURE_HEADER} header is required",
        resource=resource_info(endpoint),
        accepts=[requirements(endpoint, credits_cost)] if enabled() else [],
        extensions={"bazaar": bazaar_extension(endpoint)},
    )


def payment_required_header_value(endpoint: str, credits_cost: int) -> str:
    """base64 PaymentRequired for the PAYMENT-REQUIRED response header
    (transports-v2/http.md)."""
    return encode_payment_required_header(payment_required_model(endpoint, credits_cost))


def payment_required_body(endpoint: str, credits_cost: int) -> dict[str, Any]:
    """The 402 JSON body: the same v2 PaymentRequired payload, plus the
    sandbox rail and deprecation notes, each honestly labelled."""
    body: dict[str, Any] = payment_required_model(
        endpoint, credits_cost).model_dump(by_alias=True, exclude_none=True)
    body["sandbox"] = {
        "unit": "credits_sandbox",
        "note": ("Credits are a SANDBOX settlement unit (not money). "
                 "Free starter balance: POST /billing/trial; then send "
                 "X-API-Key. The x402 `accepts` list is the real rail."),
        "cost_credits": credits_cost,
    }
    body["v1_compat"] = {
        "status": "deprecated",
        "note": (f"Legacy x402 v1 ({X_PAYMENT_HEADER} header, x402Version 1) "
                 "is still accepted temporarily; migrate to v2 "
                 f"({PAYMENT_SIGNATURE_HEADER} header). v1 passes through the "
                 "same binding and replay guards as v2."),
    }
    if enabled() and not is_mainnet(network()):
        body["network_disclosure"] = (
            f"x402 is active on {network()} (TESTNET — settled value is NOT "
            "real money) until a funded mainnet treasury is configured.")
    if not enabled():
        body["x402_status"] = ("x402 rail not yet active on this deployment "
                               "(no treasury address configured); protocol "
                               "supported, sandbox credits available now.")
    return body


# --- server-side binding + replay guards ------------------------------------
# The facilitator verifies the SIGNATURE and settles on-chain; only this
# server knows what it actually quoted. Every acceptance therefore passes
# these guards FIRST. All failures raise PaymentBindingError with a
# machine-readable reason.

class PaymentBindingError(Exception):
    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


class _ReplayGuard:
    """Unique payment identity = (payer, nonce) of the EIP-3009 authorization.
    In-process set catches concurrent/duplicate submission; the persisted
    billing log (store.record_x402_payment) catches double settlement across
    restarts — meter() checks both."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def identity(auth: dict[str, Any]) -> str:
        return f"{str(auth.get('from', '')).lower()}:{str(auth.get('nonce', '')).lower()}"

    def check_and_reserve(self, auth: dict[str, Any]) -> str:
        ident = self.identity(auth)
        with self._lock:
            if ident in self._seen:
                raise PaymentBindingError("replay_rejected",
                                          "payment identity already used")
            self._seen[ident] = time.time()
        return ident

    def release(self, ident: str) -> None:
        # a payment that failed BEFORE settlement may be retried
        with self._lock:
            self._seen.pop(ident, None)


replay_guard = _ReplayGuard()


def _req_fields(r: Any) -> dict[str, Any]:
    if hasattr(r, "model_dump"):
        r = r.model_dump(by_alias=True, exclude_none=True)
    return {k: r.get(k) for k in ("scheme", "network", "amount", "asset",
                                  "payTo", "maxTimeoutSeconds")}


def check_binding(payload: PaymentPayload, endpoint: str, credits_cost: int,
                  method: str = RESOURCE_METHOD) -> None:
    """Exact binding of the client's payment to what THIS server quoted:
    version, method, canonical resource URL, capability, amount+asset,
    network, recipient, expiry. Raises PaymentBindingError."""
    if payload.x402_version != X402_VERSION:
        raise PaymentBindingError("invalid_x402_version",
                                  f"expected {X402_VERSION}, got {payload.x402_version}")
    if method.upper() != RESOURCE_METHOD:
        raise PaymentBindingError("method_mismatch",
                                  f"paid reads are {RESOURCE_METHOD}, got {method}")
    offered = requirements(endpoint, credits_cost)
    if _req_fields(payload.accepted) != _req_fields(offered):
        raise PaymentBindingError(
            "requirements_mismatch",
            f"accepted {_req_fields(payload.accepted)} != offered {_req_fields(offered)}")
    # canonical resource binding — the client must echo the quoted resource
    res = payload.resource
    res_url = getattr(res, "url", None) if res is not None else None
    if res_url != resource_url(endpoint):
        raise PaymentBindingError(
            "resource_mismatch",
            f"payment bound to {res_url!r}, resource is {resource_url(endpoint)!r}")
    # exact-EVM payload: EIP-3009 authorization must match the quote and be
    # inside its validity window
    inner = payload.payload if isinstance(payload.payload, dict) else {}
    auth = inner.get("authorization")
    if not isinstance(auth, dict) or not auth.get("nonce"):
        raise PaymentBindingError("invalid_payload",
                                  "missing exact-scheme authorization/nonce")
    if str(auth.get("value")) != offered.amount:
        raise PaymentBindingError("amount_mismatch",
                                  f"authorized {auth.get('value')} != quoted {offered.amount}")
    if str(auth.get("to", "")).lower() != offered.pay_to.lower():
        raise PaymentBindingError("recipient_mismatch",
                                  f"authorized recipient {auth.get('to')} != {offered.pay_to}")
    now = time.time()
    try:
        valid_after, valid_before = float(auth["validAfter"]), float(auth["validBefore"])
    except (KeyError, TypeError, ValueError):
        raise PaymentBindingError("invalid_payload", "missing/invalid validity window")
    if now < valid_after:
        raise PaymentBindingError("authorization_not_yet_valid",
                                  f"validAfter={valid_after}")
    if now >= valid_before:
        raise PaymentBindingError("authorization_expired",
                                  f"validBefore={valid_before}")


# --- facilitator (official SDK client) ---------------------------------------

def _facilitator() -> HTTPFacilitatorClientSync:
    return HTTPFacilitatorClientSync(FacilitatorConfig(url=facilitator_url()))


def decode_payment_signature(header: str) -> PaymentPayload:
    """PAYMENT-SIGNATURE is base64(JSON PaymentPayload). Rejects v1 payloads —
    those belong on the deprecated X-PAYMENT path."""
    payload = decode_payment_signature_header(header)
    if not isinstance(payload, PaymentPayload):
        raise PaymentBindingError(
            "invalid_x402_version",
            f"v1 payload on the v2 {PAYMENT_SIGNATURE_HEADER} header; "
            f"send v1 payloads on {X_PAYMENT_HEADER} (deprecated) or upgrade")
    return payload


def process_payment(payload: PaymentPayload, endpoint: str,
                    credits_cost: int, method: str = RESOURCE_METHOD,
                    protocol: str = "v2") -> dict[str, Any]:
    """Full server-side flow for one payment: binding guards → replay
    reservation → facilitator verify → facilitator settle. Returns a
    settlement record; the protected result must be served ONLY when
    record["ok"] is True."""
    check_binding(payload, endpoint, credits_cost, method=method)
    auth = payload.payload["authorization"]
    ident = replay_guard.check_and_reserve(auth)
    offered = requirements(endpoint, credits_cost)
    fac = _facilitator()
    try:
        v = fac.verify(payload, offered)
        if not getattr(v, "is_valid", False):
            replay_guard.release(ident)      # never reached settlement
            return {"ok": False, "stage": "verify",
                    "reason": getattr(v, "invalid_reason", None) or "invalid",
                    "protocol": protocol}
        s = fac.settle(payload, offered)
    except PaymentBindingError:
        replay_guard.release(ident)
        raise
    except Exception as e:
        replay_guard.release(ident)
        return {"ok": False, "stage": "facilitator",
                "reason": f"facilitator error: {e}", "protocol": protocol}
    finally:
        try:
            fac.close()
        except Exception:
            pass
    ok = bool(getattr(s, "success", False))
    net = getattr(s, "network", None) or offered.network
    record = {
        "ok": ok,
        "stage": "settle",
        "protocol": protocol,
        "x402_version": payload.x402_version,
        "endpoint": endpoint,
        "resource": resource_url(endpoint),
        "facilitator": facilitator_url(),
        "scheme": offered.scheme,
        "network": net,
        "asset": offered.asset,
        "amount_atomic": offered.amount,
        "payer": getattr(s, "payer", None) or auth.get("from"),
        "recipient": offered.pay_to,
        "transaction": getattr(s, "transaction", "") or "",
        "status": "settled" if ok else (getattr(s, "error_reason", None) or "failed"),
        "payment_identity": ident,
        "mainnet": is_mainnet(net),
        "value_note": ("REAL mainnet settlement" if is_mainnet(net) and ok else
                       "TESTNET/valueless — never counted as revenue"),
    }
    if not ok:
        replay_guard.release(ident)          # failed settlement may retry
    return record


def settle_response_header_value(record: dict[str, Any]) -> str:
    """base64 SettleResponse for the PAYMENT-RESPONSE header."""
    return encode_payment_response_header(SettleResponse(
        success=bool(record.get("ok")),
        transaction=record.get("transaction", "") or "",
        network=record.get("network", network()),
        payer=record.get("payer"),
    ))


# --- v1 LEGACY compatibility (deprecated) ------------------------------------
# The official SDK still ships v1 legacy support (X-PAYMENT header,
# x402Version 1, non-CAIP network names); we accept it temporarily so
# existing v1 clients keep working, but it is (a) labelled deprecated in
# every 402 body and (b) translated into v2 structures so it passes through
# EXACTLY the same binding + replay guards — v1 can never weaken v2
# validation (tests/test_x402_v2.py asserts this).

def decode_v1_payment_header(header: str) -> dict[str, Any]:
    """X-PAYMENT is base64(JSON v1 payment payload)."""
    return json.loads(base64.b64decode(header).decode("utf-8"))


def v1_payload_to_v2(v1: dict[str, Any], endpoint: str,
                     credits_cost: int) -> PaymentPayload:
    """Translate a v1 payload into v2 structures for guard-checking. The v1
    wire format carried scheme/network at the top level and no resource
    echo; the network name maps to CAIP-2 and the resource binds to the
    canonical URL of the endpoint the client is actually paying for."""
    if v1.get("x402Version") != 1:
        raise PaymentBindingError("invalid_x402_version",
                                  f"X-PAYMENT (v1) carried x402Version={v1.get('x402Version')}")
    net = V1_NETWORK_TO_CAIP2.get(str(v1.get("network", "")))
    if net is None:
        raise PaymentBindingError("invalid_network",
                                  f"unknown v1 network {v1.get('network')!r}")
    if net != network():
        raise PaymentBindingError("network_mismatch",
                                  f"v1 payment on {net}, service network is {network()}")
    offered = requirements(endpoint, credits_cost)
    return PaymentPayload(
        x402_version=X402_VERSION,
        accepted=offered,
        resource=resource_info(endpoint),
        payload=v1.get("payload") if isinstance(v1.get("payload"), dict) else {},
    )


def process_v1_payment_header(header: str, endpoint: str,
                              credits_cost: int) -> dict[str, Any]:
    """Deprecated v1 entry point: decode → translate → the SAME guards and
    facilitator flow as v2. The settlement record is labelled protocol=v1."""
    v1 = decode_v1_payment_header(header)
    payload = v1_payload_to_v2(v1, endpoint, credits_cost)
    return process_payment(payload, endpoint, credits_cost, protocol="v1")
