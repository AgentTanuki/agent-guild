"""x402-compatible machine payment (https://www.x402.org / Coinbase x402 spec).

Real-money rail for machine-to-machine settlement: no card, no browser, no
human checkout. A paid resource answers HTTP 402 with an `accepts` list of
payment requirements; the client retries with an `X-PAYMENT` header carrying a
signed payment payload; the server verifies (and settles) through a
FACILITATOR — the Guild never touches the client's keys.

Honesty notes, load-bearing:
  * Credits remain available and are EXPLICITLY a sandbox settlement unit
    (`credits_sandbox`) — they are not money and are labelled as such wherever
    they appear.
  * Default network is base-sepolia (testnet USDC) until a funded mainnet
    treasury exists. The active network is disclosed in every 402 body.
  * Verification/settlement is delegated to the facilitator configured in
    GUILD_X402_FACILITATOR (default: the public x402.org facilitator, which
    supports base-sepolia without an API key).

Env:
  GUILD_X402_ENABLED       "1" to advertise/accept x402 (default off until a
                           payTo address is configured)
  GUILD_X402_PAY_TO        the Guild treasury address (EVM 0x…)
  GUILD_X402_NETWORK       default "base-sepolia"
  GUILD_X402_ASSET         ERC-20 contract (default: USDC on base-sepolia)
  GUILD_X402_FACILITATOR   default "https://x402.org/facilitator"
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
from typing import Any, Optional

X402_VERSION = 1
DEFAULT_NETWORK = "base-sepolia"
# Circle USDC on Base Sepolia
DEFAULT_ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
DEFAULT_FACILITATOR = "https://x402.org/facilitator"

# 1 credit (sandbox) is priced at $0.001 (see billing.CREDIT_USD); USDC has 6
# decimals, so 1 credit == 1000 atomic USDC units on the real rail.
ATOMIC_PER_CREDIT = 1000


def enabled() -> bool:
    return (os.environ.get("GUILD_X402_ENABLED", "0") == "1"
            and bool(pay_to()))


def pay_to() -> str:
    return os.environ.get("GUILD_X402_PAY_TO", "").strip()


def network() -> str:
    return os.environ.get("GUILD_X402_NETWORK", DEFAULT_NETWORK)


def facilitator() -> str:
    return os.environ.get("GUILD_X402_FACILITATOR", DEFAULT_FACILITATOR).rstrip("/")


def requirements(resource: str, credits_cost: int,
                 description: str = "") -> dict[str, Any]:
    """The payment requirements object embedded in a 402 response."""
    return {
        "scheme": "exact",
        "network": network(),
        "maxAmountRequired": str(credits_cost * ATOMIC_PER_CREDIT),
        "resource": resource,
        "description": description or f"Agent Guild paid read: {resource}",
        "mimeType": "application/json",
        "payTo": pay_to(),
        "maxTimeoutSeconds": 300,
        "asset": os.environ.get("GUILD_X402_ASSET", DEFAULT_ASSET),
        "extra": {"name": "USDC", "version": "2"},
    }


def payment_required_body(resource: str, credits_cost: int) -> dict[str, Any]:
    """The full 402 body: real rail (x402) + sandbox rail (credits), each
    honestly labelled."""
    body: dict[str, Any] = {
        "x402Version": X402_VERSION,
        "error": "payment_required",
        "accepts": [requirements(resource, credits_cost)] if enabled() else [],
        "sandbox": {
            "unit": "credits_sandbox",
            "note": ("Credits are a SANDBOX settlement unit (not money). "
                     "Free starter balance: POST /billing/trial; then send "
                     "X-API-Key. The x402 `accepts` list is the real rail."),
            "cost_credits": credits_cost,
        },
    }
    if enabled() and network() != "base":
        body["network_disclosure"] = (
            f"x402 is active on {network()} (TESTNET — settled value is not "
            "real money) until a funded mainnet treasury is configured.")
    if not enabled():
        body["x402_status"] = ("x402 rail not yet active on this deployment "
                               "(no treasury address configured); protocol "
                               "supported, sandbox credits available now.")
    return body


def decode_payment_header(header: str) -> dict[str, Any]:
    """X-PAYMENT is base64(JSON payment payload) per the x402 spec."""
    return json.loads(base64.b64decode(header).decode("utf-8"))


def _post(path: str, body: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    req = urllib.request.Request(
        facilitator() + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def verify(payment_payload: dict[str, Any],
           reqs: dict[str, Any]) -> dict[str, Any]:
    """Facilitator verification of a client payment payload against the
    requirements. Returns the facilitator's verdict {isValid, ...}."""
    return _post("/verify", {"x402Version": X402_VERSION,
                             "paymentPayload": payment_payload,
                             "paymentRequirements": reqs})


def settle(payment_payload: dict[str, Any],
           reqs: dict[str, Any]) -> dict[str, Any]:
    """Facilitator settlement (submits the transfer on-chain). Returns
    {success, transaction, network, payer...}."""
    return _post("/settle", {"x402Version": X402_VERSION,
                             "paymentPayload": payment_payload,
                             "paymentRequirements": reqs})


def process_payment_header(header: str, resource: str,
                           credits_cost: int) -> dict[str, Any]:
    """Full server-side flow for one X-PAYMENT header: decode → verify →
    settle. Returns a settlement record (success or labelled failure)."""
    payload = decode_payment_header(header)
    reqs = requirements(resource, credits_cost)
    v = verify(payload, reqs)
    if not (v.get("isValid") or v.get("is_valid")):
        return {"ok": False, "stage": "verify", "facilitator_response": v}
    s = settle(payload, reqs)
    ok = bool(s.get("success"))
    return {"ok": ok, "stage": "settle", "network": s.get("network", network()),
            "transaction": s.get("transaction"), "payer": s.get("payer"),
            "facilitator_response": s}
