"""Independent on-chain confirmation of x402 mainnet settlements.

A facilitator response is a CLAIM, not evidence. Before a mainnet settlement
may be served or classified as real revenue, this module independently
confirms the transaction against a configurable Base JSON-RPC endpoint:

  * `eth_getTransactionReceipt(tx_hash)` exists and `status == 0x1`;
  * the receipt contains a USDC `Transfer` event log emitted BY the
    configured USDC contract (log.address == asset);
  * the Transfer's recipient (topics[2]) is the Guild's receiving address;
  * the Transfer's amount (data) equals the quoted atomic amount exactly.

If the RPC is unreachable, times out, or the receipt cannot be verified,
the answer is NOT CONFIRMED — the caller fails closed (no protected result,
no revenue). There is no facilitator-trusting fallback.

Env:
  GUILD_X402_BASE_RPC          JSON-RPC endpoint (default https://mainnet.base.org)
  GUILD_X402_CONFIRM_TIMEOUT   total seconds to wait for the receipt (default 45)
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

import httpx

DEFAULT_BASE_RPC = "https://mainnet.base.org"
# keccak256("Transfer(address,address,uint256)") — the canonical ERC-20
# Transfer event signature topic.
TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068"
                  "fc378daa952ba7f163c4a11628f55a4df523b3ef")


def rpc_url() -> str:
    return os.environ.get("GUILD_X402_BASE_RPC", DEFAULT_BASE_RPC).strip()


def confirm_timeout() -> float:
    try:
        return float(os.environ.get("GUILD_X402_CONFIRM_TIMEOUT", "45"))
    except ValueError:
        return 45.0


def _get_receipt(tx_hash: str, timeout: float = 15.0) -> Optional[dict[str, Any]]:
    """One eth_getTransactionReceipt call. Returns the receipt object, None
    if the node doesn't know the tx yet. Raises on transport/RPC errors."""
    r = httpx.post(rpc_url(), json={
        "jsonrpc": "2.0", "id": 1,
        "method": "eth_getTransactionReceipt", "params": [tx_hash],
    }, timeout=timeout)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"RPC error: {body['error']}")
    return body.get("result")


def _address_topic_matches(topic: str, address: str) -> bool:
    """An indexed address topic is the 20-byte address left-padded to 32
    bytes."""
    return (isinstance(topic, str)
            and topic.lower().removeprefix("0x").rjust(64, "0")[-40:]
            == address.lower().removeprefix("0x"))


def verify_receipt(receipt: dict[str, Any], *, asset: str, recipient: str,
                   amount_atomic: str) -> tuple[bool, str]:
    """Pure check of one receipt against the quoted settlement. Returns
    (confirmed, reason)."""
    if not isinstance(receipt, dict):
        return False, "no receipt"
    if str(receipt.get("status", "")).lower() != "0x1":
        return False, f"transaction failed (status={receipt.get('status')!r})"
    expected_amount = int(amount_atomic)
    for log in receipt.get("logs") or []:
        if not isinstance(log, dict):
            continue
        if str(log.get("address", "")).lower() != asset.lower():
            continue                     # not emitted by the USDC contract
        topics = log.get("topics") or []
        if len(topics) < 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
            continue                     # not a Transfer event
        if not _address_topic_matches(str(topics[2]), recipient):
            continue                     # transfer to someone else
        try:
            value = int(str(log.get("data", "0x0")), 16)
        except ValueError:
            continue
        if value == expected_amount:
            return True, "confirmed"
        return False, (f"amount mismatch: onchain {value}, "
                       f"quoted {expected_amount}")
    return False, ("no Transfer event from the configured USDC contract "
                   "to the configured recipient in this receipt")


def confirm_settlement(tx_hash: str, *, asset: str, recipient: str,
                       amount_atomic: str,
                       timeout_s: Optional[float] = None,
                       poll_interval: float = 3.0) -> dict[str, Any]:
    """Poll the configured RPC until the settlement is independently
    confirmed or the bounded timeout expires. NEVER raises — an unreachable
    RPC yields {"confirmed": False, "reason": "rpc_unavailable: …"} and the
    caller fails closed."""
    if not (isinstance(tx_hash, str) and tx_hash.startswith("0x")
            and len(tx_hash) == 66):
        return {"confirmed": False, "reason": f"malformed tx hash {tx_hash!r}",
                "rpc": rpc_url()}
    deadline = time.time() + (confirm_timeout() if timeout_s is None else timeout_s)
    last_reason = "receipt not yet available"
    while True:
        try:
            receipt = _get_receipt(tx_hash)
        except Exception as e:
            # transport/RPC failure — INDEPENDENT CONFIRMATION UNAVAILABLE
            last_reason = f"rpc_unavailable: {type(e).__name__}: {e}"
            receipt = None
        else:
            if receipt is not None:
                ok, reason = verify_receipt(receipt, asset=asset,
                                            recipient=recipient,
                                            amount_atomic=amount_atomic)
                return {"confirmed": ok, "reason": reason, "rpc": rpc_url(),
                        "block_number": receipt.get("blockNumber"),
                        "tx_hash": tx_hash}
        if time.time() >= deadline:
            return {"confirmed": False, "reason": last_reason,
                    "rpc": rpc_url(), "tx_hash": tx_hash}
        time.sleep(poll_interval)
