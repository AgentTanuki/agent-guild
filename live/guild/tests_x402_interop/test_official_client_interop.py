"""x402 v2 interoperability: an INDEPENDENT machine — the official x402 SDK
client with its own EVM signer, over real HTTP — discovers the price, pays,
receives the protected result and obtains verifiable settlement evidence,
with no human, no account dashboard, no copied API key, and no Agent
Guild-specific client code.

Flow proven here (mission-critical ordering):
  1. ordinary unauthenticated request → standards-compliant 402 challenge
     (PAYMENT-REQUIRED header, x402Version 2, CAIP-2 network);
  2. the OFFICIAL client constructs the payment (EIP-3009/EIP-712 signed by
     its own key) and retries automatically;
  3. the facilitator (deterministic fake with real signature recovery)
     verifies + settles;
  4. the client receives the protected response + a PAYMENT-RESPONSE header
     it can decode with the official SDK into a SettleResponse receipt.

The MCP payment path is NOT exercised: the current official x402 MCP
integration (x402.mcp) wraps servers built on the official `mcp` SDK's tool
handler signature, and the Guild's hosted MCP server is FastMCP-based — there
is no supported integration point today. HTTP is the supported transport;
this is documented in docs/CORRECTIONS_2026-07-14.md.

Everything settled here is TESTNET-SHAPED and value-less; nothing in this
suite may ever be counted as revenue (asserted below).
"""
from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from eth_account import Account

from x402 import x402Client
from x402.http import PAYMENT_REQUIRED_HEADER, PAYMENT_RESPONSE_HEADER
from x402.http.clients import x402_httpx_transport
from x402.http.utils import decode_payment_response_header
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.mechanisms.evm.signers import EthAccountSigner

# deterministic, funds-free test key — NEVER a real wallet
CLIENT_KEY = "0x" + "42" * 32
PAID_PATH = "/search?capability=interop.test"


def _official_client() -> x402Client:
    signer = EthAccountSigner(Account.from_key(CLIENT_KEY))
    client = x402Client()
    client.register("eip155:*", ExactEvmScheme(signer))
    return client


def test_unpaid_request_gets_v2_challenge(live_stack):
    r = httpx.get(live_stack["guild"] + PAID_PATH)
    assert r.status_code == 402
    hdr = r.headers.get(PAYMENT_REQUIRED_HEADER)
    assert hdr, "402 must carry PAYMENT-REQUIRED"
    challenge = json.loads(base64.b64decode(hdr))
    assert challenge["x402Version"] == 2
    req = challenge["accepts"][0]
    assert req["network"] == "eip155:84532"          # CAIP-2
    assert req["scheme"] == "exact"
    assert req["payTo"] == live_stack["pay_to"]
    assert req["amount"] == "10000"
    # bazaar discovery extension rides in the challenge
    assert "bazaar" in (challenge.get("extensions") or {})


def test_official_client_pays_and_gets_result_and_receipt(live_stack):
    async def run():
        transport = x402_httpx_transport(_official_client())
        async with httpx.AsyncClient(transport=transport) as http:
            return await http.get(live_stack["guild"] + PAID_PATH)

    r = asyncio.run(run())
    assert r.status_code == 200, r.text
    body = r.json()
    assert "results" in body                          # the protected result
    # verifiable settlement evidence, decoded with the OFFICIAL SDK
    receipt = decode_payment_response_header(
        r.headers[PAYMENT_RESPONSE_HEADER])
    assert receipt.success is True
    assert receipt.transaction.startswith("0x") and len(receipt.transaction) == 66
    assert receipt.network == "eip155:84532"
    payer = Account.from_key(CLIENT_KEY).address
    assert receipt.payer and receipt.payer.lower() == payer.lower()

    # the service records the settlement with its full identity, and REAL
    # revenue stays zero — this was a value-less testnet-shaped settlement
    revenue = httpx.get(live_stack["guild"] + "/billing/revenue").json()
    assert revenue["real_settlement"]["transactions"] == 0
    assert revenue["real_settlement"]["revenue_usd"] == 0.0
    assert revenue["testnet_settlement"]["transactions"] >= 1


def test_tampered_amount_is_rejected_end_to_end(live_stack):
    """A malicious client that alters the quoted price must not be served.
    The tampering transport sits UNDER the official x402 transport (as its
    wire transport), so it sees — and corrupts — the actual paid retry."""
    class CheapskateWire(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            sig = request.headers.get("PAYMENT-SIGNATURE")
            if sig:
                payload = json.loads(base64.b64decode(sig))
                payload["accepted"]["amount"] = "1"        # price substitution
                payload["payload"]["authorization"]["value"] = "1"
                request.headers["PAYMENT-SIGNATURE"] = base64.b64encode(
                    json.dumps(payload).encode()).decode()
            return await super().handle_async_request(request)

    async def run():
        transport = x402_httpx_transport(_official_client(), CheapskateWire())
        async with httpx.AsyncClient(transport=transport) as http:
            return await http.get(live_stack["guild"] + PAID_PATH)

    r = asyncio.run(run())
    assert r.status_code == 402
    assert r.json()["detail"]["error"] == "x402_payment_invalid"


def test_replayed_payment_is_rejected_end_to_end(live_stack):
    """Capture one successful PAYMENT-SIGNATURE off the wire and replay it."""
    captured: dict = {}

    class CapturingWire(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            sig = request.headers.get("PAYMENT-SIGNATURE")
            if sig:
                captured["sig"] = sig
            return await super().handle_async_request(request)

    async def run():
        transport = x402_httpx_transport(_official_client(), CapturingWire())
        async with httpx.AsyncClient(transport=transport) as http:
            return await http.get(live_stack["guild"] + PAID_PATH)

    assert asyncio.run(run()).status_code == 200
    assert captured.get("sig")
    replay = httpx.get(live_stack["guild"] + PAID_PATH,
                       headers={"PAYMENT-SIGNATURE": captured["sig"]})
    assert replay.status_code == 402
    detail = replay.json()["detail"]
    assert detail.get("reason") in ("replay_rejected",
                                    "double_settlement_rejected")
