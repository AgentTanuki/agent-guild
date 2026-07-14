"""Deterministic x402 v2 facilitator for CI interoperability tests.

Implements the v2 facilitator interface (POST /verify, POST /settle,
GET /supported — specs/x402-specification-v2.md §7) with REAL cryptographic
verification of the exact-EVM EIP-3009 authorization (EIP-712 signature
recovery via eth_account) — but NO blockchain: settlement returns a
synthetic transaction hash derived from the authorization nonce.

Every "settlement" through this facilitator is value-less by construction
and must never be presented as real money movement.
"""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data
from fastapi import FastAPI
from pydantic import BaseModel

NETWORK = "eip155:84532"          # Base Sepolia — TESTNET, value-less
CHAIN_ID = 84532

app = FastAPI(title="fake-x402-facilitator")
_settled: dict[str, str] = {}
_lock = threading.Lock()


class FacilitatorRequest(BaseModel):
    x402Version: int
    paymentPayload: dict[str, Any]
    paymentRequirements: dict[str, Any]


def _recover_signer(reqs: dict[str, Any], auth: dict[str, Any],
                    signature: str) -> str:
    extra = reqs.get("extra") or {}
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": extra.get("name", "USDC"),
            "version": extra.get("version", "2"),
            "chainId": int(reqs["network"].split(":", 1)[1]),
            "verifyingContract": reqs["asset"],
        },
        "message": {
            "from": auth["from"],
            "to": auth["to"],
            "value": int(auth["value"]),
            "validAfter": int(auth["validAfter"]),
            "validBefore": int(auth["validBefore"]),
            "nonce": bytes.fromhex(auth["nonce"].removeprefix("0x")),
        },
    }
    return Account.recover_message(encode_typed_data(full_message=typed),
                                   signature=signature)


def _validate(body: FacilitatorRequest) -> tuple[bool, str, str]:
    """(is_valid, invalid_reason, payer)."""
    reqs = body.paymentRequirements
    inner = body.paymentPayload.get("payload") or {}
    auth = inner.get("authorization") or {}
    sig = inner.get("signature") or ""
    if body.x402Version != 2:
        return False, "invalid_x402_version", ""
    if reqs.get("scheme") != "exact":
        return False, "unsupported_scheme", ""
    if reqs.get("network") != NETWORK:
        return False, "invalid_network", ""
    try:
        signer = _recover_signer(reqs, auth, sig)
    except Exception:
        return False, "invalid_exact_evm_payload_signature", ""
    if signer.lower() != str(auth.get("from", "")).lower():
        return False, "invalid_exact_evm_payload_signature", signer
    if str(auth.get("value")) != str(reqs.get("amount")):
        return False, "invalid_exact_evm_payload_authorization_value_mismatch", signer
    if str(auth.get("to", "")).lower() != str(reqs.get("payTo", "")).lower():
        return False, "invalid_exact_evm_payload_recipient_mismatch", signer
    now = time.time()
    if now < int(auth.get("validAfter", 0)):
        return False, "invalid_exact_evm_payload_authorization_valid_after", signer
    if now >= int(auth.get("validBefore", 0)):
        return False, "invalid_exact_evm_payload_authorization_valid_before", signer
    return True, "", signer


@app.post("/verify")
def verify(body: FacilitatorRequest):
    ok, reason, payer = _validate(body)
    if not ok:
        return {"isValid": False, "invalidReason": reason, "payer": payer or None}
    return {"isValid": True, "payer": payer}


@app.post("/settle")
def settle(body: FacilitatorRequest):
    ok, reason, payer = _validate(body)
    if not ok:
        return {"success": False, "errorReason": reason, "payer": payer or None,
                "transaction": "", "network": NETWORK}
    nonce = body.paymentPayload["payload"]["authorization"]["nonce"]
    with _lock:
        if nonce in _settled:      # EIP-3009 contracts reject nonce reuse
            return {"success": False, "errorReason": "invalid_transaction_state",
                    "payer": payer, "transaction": "", "network": NETWORK}
        tx = "0x" + hashlib.sha256(f"fake-settle:{nonce}".encode()).hexdigest()
        _settled[nonce] = tx
    return {"success": True, "payer": payer, "transaction": tx,
            "network": NETWORK}


@app.get("/supported")
def supported():
    return {"kinds": [{"x402Version": 2, "scheme": "exact", "network": NETWORK}],
            "extensions": [], "signers": {"eip155:*": []}}
