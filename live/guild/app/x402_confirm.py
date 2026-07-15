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
# Public default RPCs per CAIP-2 network, for the crash-recovery nonce oracle
# (find_authorization_used). Mainnet keeps its dedicated env override.
DEFAULT_RPC_BY_NETWORK = {
    "eip155:8453": DEFAULT_BASE_RPC,
    "eip155:84532": "https://sepolia.base.org",
}
# keccak256("Transfer(address,address,uint256)") — the canonical ERC-20
# Transfer event signature topic.
TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068"
                  "fc378daa952ba7f163c4a11628f55a4df523b3ef")
# keccak256("AuthorizationUsed(address,bytes32)") — the EIP-3009 event USDC
# emits when an authorization nonce is consumed (FiatTokenV2). Derived
# 2026-07-15 with pycryptodome keccak-256; the Transfer topic above was
# re-derived by the same method as a cross-check and matched.
AUTHORIZATION_USED_TOPIC = ("0x98de503528ee59b575ef0c0a2576a824"
                            "97bfc029a5685b209e9ec333479b10a5")
# 4-byte selector of authorizationState(address,bytes32) — EIP-3009's
# definitive nonce-consumed view (same derivation + cross-check).
AUTHORIZATION_STATE_SELECTOR = "0xe94a0102"
# Legacy bounded look-back for locating the AuthorizationUsed tx hash when NO
# anchored start block was persisted (pre-anchor records only): public RPCs
# reject unbounded eth_getLogs ranges. 90k blocks ≈ 2 days on Base. Anchored
# recovery (from_block persisted with the payment-identifier record BEFORE
# settlement) scans FORWARD from the anchor instead and has no such horizon.
_LOG_LOOKBACK_BLOCKS = 90_000
_LOG_CHUNK_BLOCKS = 9_000
# Recovery anchor: how many blocks BEFORE the observed chain head the
# persisted safe starting block is placed (reorg headroom, ~1 min on Base).
RECOVERY_ANCHOR_MARGIN_BLOCKS = 30
# Hard bound on anchored forward scans: enough chunks to cover months of
# Base blocks, still finite so a poisoned anchor cannot spin forever.
_MAX_ANCHORED_CHUNKS = 4_000


def rpc_url() -> str:
    return os.environ.get("GUILD_X402_BASE_RPC", DEFAULT_BASE_RPC).strip()


def rpc_url_for(network: Optional[str]) -> str:
    """RPC endpoint for a CAIP-2 network. The mainnet env override wins for
    mainnet; other known networks use their public default. Unknown networks
    return "" — callers must treat that as 'oracle unavailable', never guess."""
    if not network or network == "eip155:8453":
        return rpc_url()
    return DEFAULT_RPC_BY_NETWORK.get(network, "")


def confirm_timeout() -> float:
    try:
        return float(os.environ.get("GUILD_X402_CONFIRM_TIMEOUT", "45"))
    except ValueError:
        return 45.0


def _rpc_call(url: str, method: str, params: list[Any],
              timeout: float = 15.0) -> Any:
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1,
                              "method": method, "params": params},
                   timeout=timeout)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"RPC error: {body['error']}")
    return body.get("result")


def _pad_address_topic(addr: str) -> str:
    return "0x" + addr.lower().removeprefix("0x").rjust(64, "0")


def current_block(network: Optional[str] = None,
                  timeout: float = 15.0) -> Optional[int]:
    """The chain head for `network`, or None when the RPC cannot answer.
    Used to persist a SAFE recovery starting block with the payment-
    identifier record BEFORE a mainnet settlement — callers must fail closed
    (refuse to settle) on None rather than create an unanchorable ambiguity."""
    url = rpc_url_for(network)
    if not url:
        return None
    try:
        return int(str(_rpc_call(url, "eth_blockNumber", [],
                                 timeout=timeout)), 16)
    except Exception:
        return None


def find_authorization_used(payer: str, nonce: str, *, asset: str,
                            network: Optional[str] = None,
                            timeout: float = 15.0,
                            from_block: Optional[int] = None,
                            ) -> dict[str, Any]:
    """The crash-recovery nonce oracle: has this EIP-3009 (payer, nonce)
    authorization been consumed on-chain?

    Consulted ONLY when a durable payment-identifier record was left in the
    ambiguous `settling` state by a crash — the one situation where the
    facilitator may or may not have settled and neither retrying blindly nor
    failing forever is acceptable.

    Returns (never raises):
      {"used": True,  "definitive": True, "transaction": "0x…"|None}
      {"used": False, "definitive": True}
      {"used": None,  "definitive": False, "reason": "…"}   # oracle unavailable

    Method: `authorizationState(address,bytes32)` eth_call on the USDC
    contract (EIP-3009's canonical consumed-nonce view) decides used/unused
    definitively; when used, a bounded eth_getLogs scan over the
    AuthorizationUsed(payer, nonce) event recovers the transaction hash so
    the settlement can be independently confirmed and receipted.

    `from_block` is the SAFE starting block persisted with the payment-
    identifier record before the settlement was attempted: the scan then
    runs FORWARD from that anchor to the head — however far in the past the
    settlement now is (no latest-90k horizon). Without an anchor (legacy
    records only) the scan degrades to the bounded latest-90k look-back. A
    missing hash degrades to used-without-tx — the caller decides what that
    is enough for."""
    url = rpc_url_for(network)
    if not url:
        return {"used": None, "definitive": False,
                "reason": f"no RPC configured for network {network!r}"}
    if not (isinstance(nonce, str) and nonce.startswith("0x")
            and len(nonce) == 66):
        return {"used": None, "definitive": False,
                "reason": f"malformed nonce {str(nonce)[:80]!r}"}
    try:
        data = (AUTHORIZATION_STATE_SELECTOR
                + _pad_address_topic(payer)[2:]
                + nonce[2:].lower())
        state = _rpc_call(url, "eth_call",
                          [{"to": asset, "data": data}, "latest"],
                          timeout=timeout)
        used = bool(int(str(state or "0x0"), 16))
    except Exception as e:
        return {"used": None, "definitive": False,
                "reason": f"rpc_unavailable: {type(e).__name__}: {e}"}
    if not used:
        return {"used": False, "definitive": True}
    # nonce consumed — recover the tx hash from the AuthorizationUsed event
    tx: Optional[str] = None
    topics = [AUTHORIZATION_USED_TOPIC, _pad_address_topic(payer),
              nonce.lower()]

    def _scan(lo_blk: int, hi_blk: int) -> Optional[str]:
        logs = _rpc_call(url, "eth_getLogs", [{
            "address": asset,
            "fromBlock": hex(lo_blk), "toBlock": hex(hi_blk),
            "topics": topics,
        }], timeout=timeout)
        for log in logs or []:
            if isinstance(log, dict) and log.get("transactionHash"):
                return str(log["transactionHash"])
        return None

    try:
        latest = int(str(_rpc_call(url, "eth_blockNumber", [],
                                   timeout=timeout)), 16)
        if from_block is not None:
            # anchored recovery: FORWARD from the persisted safe block —
            # the event sits just after the anchor, so the first chunks
            # almost always find it, however old the settlement now is.
            frm = max(0, int(from_block))
            chunks = 0
            while frm <= latest and tx is None:
                if chunks >= _MAX_ANCHORED_CHUNKS:
                    break
                hi = min(latest, frm + _LOG_CHUNK_BLOCKS - 1)
                tx = _scan(frm, hi)
                frm = hi + 1
                chunks += 1
        else:
            # legacy (no anchor persisted): bounded latest-90k look-back
            lo = max(0, latest - _LOG_LOOKBACK_BLOCKS)
            frm = latest
            while frm > lo and tx is None:
                chunk_from = max(lo, frm - _LOG_CHUNK_BLOCKS + 1)
                tx = _scan(chunk_from, frm)
                frm = chunk_from - 1
    except Exception:
        tx = None                       # used is still definitive without it
    return {"used": True, "definitive": True, "transaction": tx}


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
