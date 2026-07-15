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

EXACT-RESOURCE BINDING (machine-commerce closure sprint, 2026-07-15).
Payments used to be bound to a per-CAPABILITY canonical URL — so a 402 from
`GET /search?capability=code-review` quoted `resource.url = …/check` (a
different route), `{id}` templates never resolved to the agent actually being
read, and query parameters were not part of the binding at all. Every quote
and every acceptance is now bound to a `PaidRequest` (app/payments.py): the
TRUSTED configured public origin (never a Host/forwarded header), the actual
HTTP method, the actual concrete path, and the canonically-encoded
result-affecting query parameters — plus amount, asset, network, recipient
and the EIP-3009 validity window + single-use nonce.

Honesty notes, load-bearing:
  * Credits remain available and are EXPLICITLY a sandbox settlement unit
    (`credits_sandbox`) — not money, labelled as such wherever they appear.
  * Default network is eip155:84532 (Base Sepolia — TESTNET, value-less)
    until a funded mainnet treasury exists. Every 402 discloses the network.
  * REAL revenue is counted only from successful settlements on a MAINNET
    network with a transaction hash (store.revenue → real_settlement).
    Testnet/mocked settlements are recorded separately and never counted.
  * The legacy v1 protocol (X-PAYMENT header, x402Version 1) is NO LONGER
    accepted on priced HTTP routes: a v1 payload carries no resource echo, so
    it cannot be bound to the actual semantic request — accepting it would
    reopen the cross-resource substitution hole this module exists to close.
    The v1→v2 translation survives ONLY for the A2A x402 extension (v0.1),
    where the payment is bound server-side to the task's stored quote
    (taskId correlation), which restores exactly the binding v1's wire format
    lacks.

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
from typing import TYPE_CHECKING, Any, Optional

from x402.http import (
    PAYMENT_REQUIRED_HEADER,     # "PAYMENT-REQUIRED"   (402 → client)
    PAYMENT_RESPONSE_HEADER,     # "PAYMENT-RESPONSE"   (settlement → client)
    PAYMENT_SIGNATURE_HEADER,    # "PAYMENT-SIGNATURE"  (client → server)
    X_PAYMENT_HEADER,            # "X-PAYMENT"          (v1 legacy, REJECTED on HTTP)
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

from . import x402_cdp
from . import x402_confirm

if TYPE_CHECKING:  # circular-import-free type hints only
    from .payments import PaidRequest

X402_VERSION = 2
DEFAULT_NETWORK = "eip155:84532"            # Base Sepolia (CAIP-2)
# Canonical USDC contracts (verified against the x402 SDK's NETWORK_CONFIGS
# and Circle's deployments, 2026-07-14). The mainnet address is the FULL
# 40-hex-char contract — beware truncated copies in prose.
USDC_BY_NETWORK = {
    "eip155:84532": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",  # Base Sepolia
    "eip155:8453": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",   # Base mainnet
}
# EIP-712 domain names are contract metadata and differ between Circle's
# testnet and mainnet USDC deployments.  A client and facilitator can agree on
# the wrong string and still recover a signature, but the token contract will
# reject it at settlement.  Keep this network-bound just like the asset.
USDC_EIP712_NAME_BY_NETWORK = {
    "eip155:84532": "USDC",
    "eip155:8453": "USD Coin",
}
DEFAULT_ASSET = USDC_BY_NETWORK["eip155:84532"]
# The dedicated Agent Guild treasury (`agent-guild-treasury`, provisioned in
# CDP 2026-07-14). This is a PUBLIC address, not a secret. Mainnet payments
# are PINNED to it: any other GUILD_X402_PAY_TO on eip155:8453 fails closed,
# so a mistyped or maliciously swapped Render env var can never redirect
# real settlements. Rotating the treasury is a reviewed code change on
# purpose.
MAINNET_TREASURY = "0xaa4E3ba0Eb5f564cAb54dDC08f5BaAfb3D4cA8E5"
# The unauthenticated x402.org facilitator is TESTNET-ONLY (official x402
# docs); Base mainnet uses the authenticated Coinbase CDP facilitator.
TESTNET_FACILITATOR = "https://x402.org/facilitator"
DEFAULT_FACILITATOR = TESTNET_FACILITATOR
DEFAULT_FACILITATOR_BY_NETWORK = {
    "eip155:84532": TESTNET_FACILITATOR,
    "eip155:8453": x402_cdp.CDP_FACILITATOR_URL,
}
DEFAULT_HOST = "https://agent-guild-5d5r.onrender.com"

# Networks whose successful settlement is REAL value. Everything else
# (testnets, local fakes) is value-less and must never count as revenue.
MAINNET_NETWORKS = frozenset({
    "eip155:8453",      # Base mainnet
    "eip155:43114",     # Avalanche mainnet
    "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",  # Solana mainnet
})

# v1 legacy network names → CAIP-2 (A2A x402 v0.1 uses the legacy names).
V1_NETWORK_TO_CAIP2 = {
    "base-sepolia": "eip155:84532",
    "base": "eip155:8453",
    "avalanche-fuji": "eip155:43113",
    "avalanche": "eip155:43114",
}
CAIP2_TO_V1_NETWORK = {v: k for k, v in V1_NETWORK_TO_CAIP2.items()}

# 1 credit (sandbox) is priced at $0.001 (see billing.CREDIT_USD); USDC has 6
# decimals, so 1 credit == 1000 atomic USDC units on the real rail.
ATOMIC_PER_CREDIT = 1000

# One EXAMPLE resource per priced capability — used ONLY by discovery surfaces
# (bazaar catalogue, machine manifests). Actual payment binding is per-request
# (PaidRequest), never per-capability.
EXAMPLE_RESOURCE_PATHS = {
    "best_agent": "/check?capability=code-review",
    "reputation": "/agents/{id}/reputation",
    "evidence": "/agents/{id}/evidence",
    "risk_score": "/agents/{id}/risk-score",
    "fraud_check": "/agents/{id}/flags",
}
# All priced reads are canonically GETs; the method is part of the binding.
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
    return os.environ.get("GUILD_X402_ASSET",
                          USDC_BY_NETWORK.get(network(), DEFAULT_ASSET))


def facilitator_url() -> str:
    return os.environ.get(
        "GUILD_X402_FACILITATOR",
        DEFAULT_FACILITATOR_BY_NETWORK.get(network(), DEFAULT_FACILITATOR),
    ).rstrip("/")


def _facilitator_host(url: str = "") -> str:
    from urllib.parse import urlparse
    return urlparse(url or facilitator_url()).hostname or ""


_ADDRESS_RE = None


def _valid_evm_address(addr: str) -> bool:
    global _ADDRESS_RE
    if _ADDRESS_RE is None:
        import re
        _ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
    return bool(_ADDRESS_RE.match(addr)) and int(addr, 16) != 0


def config_errors() -> list[str]:
    """Fail-closed configuration validation for the x402 rail. Empty list ==
    valid. Called at startup (app lifespan refuses to boot a misconfigured
    MAINNET rail) and again at payment time. Mainnet (real money) demands:

      * the authenticated CDP facilitator — never the testnet x402.org one;
      * CDP API credentials present (never validated by echoing them);
      * a structurally valid, non-zero receiving address;
      * the canonical Base-mainnet USDC contract — never the testnet one;
      * an https public resource origin that is not local/private;
      * an https Base RPC endpoint for INDEPENDENT settlement confirmation.
    """
    if not enabled():
        return []
    errs: list[str] = []
    net = network()
    pay = pay_to()
    if not _valid_evm_address(pay):
        errs.append("GUILD_X402_PAY_TO is not a valid non-zero EVM address")
    if not is_mainnet(net):
        return errs
    # --- mainnet-only hard requirements --------------------------------
    fac_host = _facilitator_host()
    if fac_host != x402_cdp.CDP_FACILITATOR_HOST:
        errs.append(
            f"mainnet facilitator must be the authenticated CDP facilitator "
            f"({x402_cdp.CDP_FACILITATOR_HOST}); configured host is "
            f"{fac_host or 'invalid'} — the x402.org facilitator is "
            "testnet-only")
    if not facilitator_url().startswith("https://"):
        errs.append("mainnet facilitator URL must be https")
    if not x402_cdp.credentials_configured():
        errs.append("CDP_API_KEY_ID / CDP_API_KEY_SECRET are not configured "
                    "— the CDP facilitator authenticates every /verify and "
                    "/settle request")
    if pay and pay.lower() != MAINNET_TREASURY.lower():
        errs.append("mainnet recipient is PINNED to the agent-guild-treasury "
                    f"address {MAINNET_TREASURY}; GUILD_X402_PAY_TO is set "
                    "to a different address")
    expected_usdc = USDC_BY_NETWORK["eip155:8453"]
    if asset().lower() != expected_usdc.lower():
        detail = ("the TESTNET USDC contract"
                  if asset().lower() == USDC_BY_NETWORK["eip155:84532"].lower()
                  else f"{asset()!r}")
        errs.append(f"mainnet asset must be Base USDC {expected_usdc}; "
                    f"configured asset is {detail}")
    host = public_host()
    from urllib.parse import urlparse
    parsed = urlparse(host)
    if parsed.scheme != "https" or not parsed.hostname:
        errs.append(f"public resource origin {host!r} must be a valid https "
                    "origin on mainnet")
    elif (parsed.hostname in ("localhost", "0.0.0.0")
          or parsed.hostname.startswith(("127.", "10.", "192.168."))):
        errs.append(f"public resource origin {host!r} is local/private — "
                    "mainnet payments would be bound to unreachable "
                    "resource URLs")
    if not x402_confirm.rpc_url().startswith("https://"):
        errs.append("GUILD_X402_BASE_RPC must be an https JSON-RPC endpoint "
                    "— independent mainnet confirmation is mandatory")
    return errs


def assert_config_valid() -> None:
    """Raise (fail closed) if the enabled rail is misconfigured."""
    errs = config_errors()
    if errs:
        raise RuntimeError("x402 rail misconfigured: " + "; ".join(errs))


def readiness() -> dict[str, Any]:
    """Non-secret, machine-readable payment-readiness. NEVER includes
    credentials, key material, or the RPC/facilitator beyond their hosts."""
    from urllib.parse import urlparse
    errs = config_errors()
    return {
        "rail": "x402",
        "version": X402_VERSION,
        "enabled": enabled(),
        "network": network(),
        "mainnet": is_mainnet(network()),
        "asset": asset(),
        "recipient": pay_to() or None,
        "recipient_is_pinned_treasury": (
            pay_to().lower() == MAINNET_TREASURY.lower()
            if is_mainnet(network()) and pay_to() else None),
        "facilitator_host": _facilitator_host() or None,
        "facilitator_authenticated": (
            _facilitator_host() == x402_cdp.CDP_FACILITATOR_HOST
            and x402_cdp.credentials_configured()),
        "independent_confirmation_rpc_host": (
            urlparse(x402_confirm.rpc_url()).hostname
            if is_mainnet(network()) else None),
        "config_valid": not errs,
        "config_errors": errs,
        "extensions": ["bazaar", "payment-identifier", "offer-receipt",
                       "io.agent-guild/evidence"],
        "transports": {
            "http": "PAYMENT-REQUIRED / PAYMENT-SIGNATURE / PAYMENT-RESPONSE "
                    "headers (x402 v2 HTTP transport)",
            "a2a": "A2A x402 extension v0.1 "
                   "(https://github.com/google-a2a/a2a-x402/v0.1) at POST /a2a",
            "mcp": "x402 MCP flow (payment-required tool error + "
                   "_meta['x402/payment'] retry) at /mcp",
        },
        "revenue_policy": ("real revenue counts ONLY mainnet settlements "
                           "independently confirmed on-chain (receipt status, "
                           "USDC contract, recipient, exact amount)"),
    }


def public_host() -> str:
    """The TRUSTED canonical public origin for every quoted resource URL.
    Comes ONLY from configuration (GUILD_PUBLIC_HOST) — never from a Host,
    X-Forwarded-Host or any other request header an attacker controls."""
    return os.environ.get("GUILD_PUBLIC_HOST", DEFAULT_HOST).rstrip("/")


def example_resource_url(endpoint: str) -> str:
    """A representative resource URL for DISCOVERY surfaces only (bazaar
    catalogue, manifests). Payment binding never uses this — it binds to the
    concrete PaidRequest."""
    return public_host() + EXAMPLE_RESOURCE_PATHS.get(
        endpoint, f"/x402/resources/{endpoint}")


def requirements(credits_cost: int) -> PaymentRequirements:
    """The v2 payment requirements the Guild quotes for one priced request."""
    net = network()
    return PaymentRequirements(
        scheme="exact",
        network=net,
        amount=str(credits_cost * ATOMIC_PER_CREDIT),
        asset=asset(),
        pay_to=pay_to(),
        max_timeout_seconds=300,
        extra={"name": USDC_EIP712_NAME_BY_NETWORK.get(net, "USDC"),
               "version": "2"},
    )


def resource_info(preq: "PaidRequest") -> ResourceInfo:
    return ResourceInfo(
        url=preq.resource_url,
        description=f"Agent Guild paid read: {preq.operation}",
        mime_type="application/json",
    )


# Bazaar discovery extension (x402 specs/extensions/bazaar.md): machine-
# readable endpoint specifications inside the 402 challenge, so facilitator
# catalogues can index the Guild's paid trust operations without a human.
_BAZAAR_OUTPUT = {
    "best_agent": {"verdict": "hire", "best": {"agent_id": "…", "score": 0.93}},
    "reputation": {"score": 0.9, "confidence": 0.8},
    "evidence": {"attestations": [], "receipts": []},
    "risk_score": {"risk": 12, "recommendation": "hire"},
    "fraud_check": {"suspicion": 0.02, "flags": []},
}


def bazaar_extension(preq: "PaidRequest") -> dict[str, Any]:
    query = dict(preq.query)
    info: dict[str, Any] = {
        "input": {"type": "http", "method": preq.method,
                  **({"queryParams": query} if query else {})},
        "output": {"type": "json",
                   "example": _BAZAAR_OUTPUT.get(preq.operation, {})},
    }
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "input": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "const": "http"},
                    "method": {"type": "string", "enum": [preq.method]},
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


def payment_required_model(preq: "PaidRequest", credits_cost: int,
                           extensions: Optional[dict[str, Any]] = None,
                           ) -> PaymentRequired:
    """The v2 PaymentRequired for ONE concrete request. `resource.url` is the
    exact semantic request being paid for (trusted origin + actual path +
    canonical result-affecting query), never a capability template."""
    exts: dict[str, Any] = {"bazaar": bazaar_extension(preq)}
    if extensions:
        exts.update(extensions)
    return PaymentRequired(
        x402_version=X402_VERSION,
        error=f"{PAYMENT_SIGNATURE_HEADER} header is required",
        resource=resource_info(preq),
        accepts=[requirements(credits_cost)] if enabled() else [],
        extensions=exts,
    )


def payment_required_header_value(model: PaymentRequired) -> str:
    """base64 PaymentRequired for the PAYMENT-REQUIRED response header
    (transports-v2/http.md)."""
    return encode_payment_required_header(model)


def payment_required_body(preq: "PaidRequest", credits_cost: int,
                          model: Optional[PaymentRequired] = None,
                          ) -> dict[str, Any]:
    """The 402 JSON body: the same v2 PaymentRequired payload, plus the
    sandbox rail and deprecation notes, each honestly labelled."""
    if model is None:
        model = payment_required_model(preq, credits_cost)
    body: dict[str, Any] = model.model_dump(by_alias=True, exclude_none=True)
    body["sandbox"] = {
        "unit": "credits_sandbox",
        "note": ("Credits are a SANDBOX settlement unit (not money). "
                 "Free starter balance: POST /billing/trial; then send "
                 "X-API-Key. The x402 `accepts` list is the real rail."),
        "cost_credits": credits_cost,
    }
    body["v1_compat"] = {
        "status": "removed",
        "note": (f"Legacy x402 v1 ({X_PAYMENT_HEADER} header, x402Version 1) "
                 "is NOT accepted on priced HTTP routes: v1 payloads carry no "
                 "resource echo, so they cannot be bound to the exact request "
                 f"being paid for. Use v2 ({PAYMENT_SIGNATURE_HEADER} header) "
                 "and echo the `resource` object from this challenge."),
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
    restarts — the gateway checks both."""

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


def check_binding(payload: PaymentPayload, preq: "PaidRequest",
                  credits_cost: int, method: Optional[str] = None) -> None:
    """Exact binding of the client's payment to what THIS server quoted for
    THIS request: version, actual method, exact resource URL (trusted origin +
    concrete path + canonical query), amount+asset, network, recipient,
    expiry + nonce. Raises PaymentBindingError."""
    if payload.x402_version != X402_VERSION:
        raise PaymentBindingError("invalid_x402_version",
                                  f"expected {X402_VERSION}, got {payload.x402_version}")
    actual_method = (method or preq.method).upper()
    if actual_method != preq.method.upper():
        raise PaymentBindingError("method_mismatch",
                                  f"resource is {preq.method}, got {actual_method}")
    offered = requirements(credits_cost)
    if _req_fields(payload.accepted) != _req_fields(offered):
        raise PaymentBindingError(
            "requirements_mismatch",
            f"accepted {_req_fields(payload.accepted)} != offered {_req_fields(offered)}")
    # exact-resource binding — the client must echo the resource of the
    # request it is actually paying for; path substitution, query mutation and
    # agent-id substitution all change this URL and fail here.
    res = payload.resource
    res_url = getattr(res, "url", None) if res is not None else None
    if res_url != preq.resource_url:
        raise PaymentBindingError(
            "resource_mismatch",
            f"payment bound to {res_url!r}, resource is {preq.resource_url!r}")
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
    """The facilitator client. The CDP facilitator is AUTHENTICATED: every
    /verify and /settle carries a fresh request-bound Bearer JWT via the
    x402 SDK's AuthProvider hook (app/x402_cdp.py). The unauthenticated
    x402.org facilitator remains for testnet only — config_errors() rejects
    it for mainnet."""
    url = facilitator_url()
    if _facilitator_host(url) == x402_cdp.CDP_FACILITATOR_HOST:
        return HTTPFacilitatorClientSync(FacilitatorConfig(
            url=url, auth_provider=x402_cdp.auth_provider()))
    return HTTPFacilitatorClientSync(FacilitatorConfig(url=url))


def decode_payment_signature(header: str) -> PaymentPayload:
    """PAYMENT-SIGNATURE is base64(JSON PaymentPayload). Rejects v1 payloads —
    v1 is not accepted on HTTP (no resource echo, no exact binding)."""
    payload = decode_payment_signature_header(header)
    if not isinstance(payload, PaymentPayload):
        raise PaymentBindingError(
            "invalid_x402_version",
            "v1 payload on the v2 "
            f"{PAYMENT_SIGNATURE_HEADER} header; v1 is not accepted on "
            "priced HTTP routes — upgrade to x402 v2")
    return payload


def process_payment(payload: PaymentPayload, preq: "PaidRequest",
                    credits_cost: int, method: Optional[str] = None,
                    protocol: str = "v2") -> dict[str, Any]:
    """Full server-side flow for one payment: config fail-closed → binding
    guards → replay reservation → facilitator verify → facilitator settle →
    (mainnet) INDEPENDENT on-chain confirmation. Returns a settlement
    record; the protected result must be served ONLY when record["ok"] is
    True — and on mainnet ok requires the independent confirmation, never
    the facilitator's word alone."""
    cfg_errs = config_errors()
    if cfg_errs:
        raise PaymentBindingError("x402_misconfigured", "; ".join(cfg_errs))
    check_binding(payload, preq, credits_cost, method=method)
    auth = payload.payload["authorization"]
    ident = replay_guard.check_and_reserve(auth)
    offered = requirements(credits_cost)
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
    tx = getattr(s, "transaction", "") or ""
    if ok and not (isinstance(tx, str) and tx.startswith("0x") and len(tx) == 66):
        # a "successful" settlement without a well-formed transaction hash is
        # a malformed facilitator response — fail closed
        ok, tx = False, tx if isinstance(tx, str) else ""
        malformed = "facilitator claimed success without a valid tx hash"
    else:
        malformed = None
    record = {
        "ok": ok,
        "stage": "settle",
        "protocol": protocol,
        "x402_version": payload.x402_version,
        "endpoint": preq.operation,
        "resource": preq.resource_url,
        "request_hash": preq.request_hash,
        "facilitator": facilitator_url(),
        "scheme": offered.scheme,
        "network": net,
        "asset": offered.asset,
        "amount_atomic": offered.amount,
        "payer": getattr(s, "payer", None) or auth.get("from"),
        "recipient": offered.pay_to,
        "transaction": tx,
        "status": ("settled" if ok else
                   malformed or getattr(s, "error_reason", None) or "failed"),
        "payment_identity": ident,
        "mainnet": is_mainnet(net),
        "confirmed": False,
        "value_note": "TESTNET/valueless — never counted as revenue",
    }
    if ok and is_mainnet(net):
        # A mainnet facilitator response alone is NEVER sufficient: confirm
        # the Base transaction receipt and the USDC Transfer event
        # (status, contract, recipient, exact amount) on an independent RPC.
        conf = x402_confirm.confirm_settlement(
            tx, asset=offered.asset, recipient=offered.pay_to,
            amount_atomic=offered.amount)
        record["confirmation"] = {k: conf.get(k) for k in
                                  ("confirmed", "reason", "block_number")}
        if conf.get("confirmed"):
            record["status"] = "settled_confirmed"
            record["confirmed"] = True
            record["value_note"] = ("REAL mainnet settlement — independently "
                                    "confirmed on-chain")
        else:
            # fail closed: the identity stays reserved (the authorization may
            # have settled on-chain); the caller can re-present the SAME
            # payment and recovery re-runs confirmation (see payments.py).
            record["ok"] = False
            record["status"] = "settled_unconfirmed"
            record["value_note"] = ("mainnet settlement NOT independently "
                                    "confirmed — result withheld, never "
                                    "counted as revenue")
        return record
    if not ok:
        replay_guard.release(ident)          # failed settlement may retry
    return record


def settle_response_model(record: dict[str, Any],
                          extensions: Optional[dict[str, Any]] = None,
                          ) -> SettleResponse:
    return SettleResponse(
        success=bool(record.get("ok")),
        transaction=record.get("transaction", "") or "",
        network=record.get("network", network()),
        payer=record.get("payer"),
        extensions=extensions or None,
    )


def settle_response_header_value(record: dict[str, Any],
                                 extensions: Optional[dict[str, Any]] = None,
                                 ) -> str:
    """base64 SettleResponse for the PAYMENT-RESPONSE header. `extensions`
    carries the signed receipt (offer-receipt) + the Guild evidence
    attachment."""
    return encode_payment_response_header(
        settle_response_model(record, extensions))


# --- v1 → v2 translation (A2A x402 extension v0.1 ONLY) -----------------------
# The A2A x402 extension v0.1 (official Google spec) carries v1-shaped
# payloads ({x402Version: 1, scheme, network, payload}). On that transport the
# server binds the payment to the TASK's stored quote (taskId correlation), so
# the missing wire-level resource echo is supplied server-side. Raw HTTP v1
# (X-PAYMENT header) is no longer accepted anywhere.

def decode_v1_payment_header(header: str) -> dict[str, Any]:
    """X-PAYMENT is base64(JSON v1 payment payload)."""
    return json.loads(base64.b64decode(header).decode("utf-8"))


def v1_payload_to_v2(v1: dict[str, Any], preq: "PaidRequest",
                     credits_cost: int) -> PaymentPayload:
    """Translate a v1-shaped payload into v2 structures for guard-checking.
    The resource binds to the PaidRequest the server itself stored for the
    correlated task — the client cannot influence it."""
    if v1.get("x402Version") != 1:
        raise PaymentBindingError("invalid_x402_version",
                                  f"payload carried x402Version={v1.get('x402Version')}")
    net = V1_NETWORK_TO_CAIP2.get(str(v1.get("network", "")),
                                  str(v1.get("network", "")))
    if net not in V1_NETWORK_TO_CAIP2.values():
        raise PaymentBindingError("invalid_network",
                                  f"unknown v1 network {v1.get('network')!r}")
    if net != network():
        raise PaymentBindingError("network_mismatch",
                                  f"payment on {net}, service network is {network()}")
    offered = requirements(credits_cost)
    return PaymentPayload(
        x402_version=X402_VERSION,
        accepted=offered,
        resource=resource_info(preq),
        payload=v1.get("payload") if isinstance(v1.get("payload"), dict) else {},
    )
