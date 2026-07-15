"""Payment crash recovery beyond the RPC log window (pre-mainnet pass).

Defect reproduced: the crash-recovery nonce oracle located the settling
transaction with a bounded eth_getLogs scan over the LATEST 90,000 blocks
(~2 days on Base). A recovery attempted later than that could prove the
nonce was consumed but never locate the transaction — so a paid buyer was
failed closed FOREVER on mainnet.

Fix under test:
  * BEFORE a mainnet facilitator settlement, a safe starting Base block is
    persisted with the payment-identifier record;
  * if that anchor cannot be established, the settlement itself fails closed
    BEFORE the facilitator is contacted (no ambiguity is ever created);
  * ambiguous recovery scans FORWARD from the anchored block for the exact
    payer+nonce AuthorizationUsed event — however far in the past it now is —
    then independently confirms the transaction and serves the cached result;
  * an RPC that cannot establish the on-chain state still fails closed.
"""
import json
import threading
import time
import uuid

import pytest

from app import payments, x402, x402_confirm
from app import state as app_state
from app.store import Store
from tests.test_x402_v2 import FakeFacilitator, make_payload
from tests.test_payment_identifier import _with_pid, _pid
from tests.test_payment_crash_recovery import NonceTrackingFacilitator
from tests.test_x402_cdp_settlement import (FAKE_KEY_ID, FAKE_SECRET,
                                            _receipt)

MAINNET = "eip155:8453"
PAY_TO = x402.MAINNET_TREASURY
PAYER = "0x" + "22" * 20


@pytest.fixture(params=["json", "sqlite"])
def mainnet_store(request, tmp_path, monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_X402_NETWORK", MAINNET)
    monkeypatch.setenv("CDP_API_KEY_ID", FAKE_KEY_ID)
    monkeypatch.setenv("CDP_API_KEY_SECRET", FAKE_SECRET)
    monkeypatch.delenv("GUILD_X402_ASSET", raising=False)
    monkeypatch.delenv("GUILD_X402_FACILITATOR", raising=False)
    monkeypatch.delenv("GUILD_X402_BASE_RPC", raising=False)
    monkeypatch.setenv("GUILD_X402_CONFIRM_TIMEOUT", "0")
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_RECOVERY_LEASE_S", "0")
    monkeypatch.setenv("GUILD_STORE", request.param)
    data = str(tmp_path / "guild.json")

    def boot():
        s = Store(path=data)
        monkeypatch.setattr(app_state, "store", s)
        payments._inflight_reset_for_process_restart()
        x402.replay_guard._seen.clear()
        return s

    yield boot


@pytest.fixture
def fac(monkeypatch):
    f = NonceTrackingFacilitator(network=MAINNET)
    monkeypatch.setattr(x402, "_facilitator", lambda: f)
    return f


def _confirm_ok(monkeypatch):
    monkeypatch.setattr(x402_confirm, "_get_receipt",
                        lambda tx, timeout=15.0: _receipt())


def _preq():
    return payments.search_request("deep-" + uuid.uuid4().hex[:12])


def _serve(payload, preq):
    auth = payments.authorize(preq, payment=payload, protocol="v2",
                              transport="http")
    assert auth.mode == "x402" and auth.settled is not None
    body = json.dumps({"ok": True, "resource": preq.resource_url}).encode()
    auth.settled.finalize(body)
    return body


class FakeChain:
    """A deterministic Base JSON-RPC stand-in with FULL history: the
    AuthorizationUsed event sits at `event_block`, the chain head at
    `latest`. eth_getLogs answers any bounded range honestly — exactly what
    a real archive RPC does. No 90k-window courtesy: a scan anchored at
    'latest - 90k' simply never sees an event 200k blocks back."""

    def __init__(self, payer, nonce, event_block, latest, tx):
        self.payer, self.nonce = payer.lower(), nonce.lower()
        self.event_block, self.latest, self.tx = event_block, latest, tx
        self.get_logs_calls = []

    def rpc(self, url, method, params, timeout=15.0):
        if method == "eth_blockNumber":
            return hex(self.latest)
        if method == "eth_call":
            data = params[0]["data"]
            # authorizationState(payer, nonce) → consumed
            assert data.startswith(x402_confirm.AUTHORIZATION_STATE_SELECTOR)
            return "0x1"
        if method == "eth_getLogs":
            q = params[0]
            lo, hi = int(q["fromBlock"], 16), int(q["toBlock"], 16)
            self.get_logs_calls.append((lo, hi))
            topics = q["topics"]
            assert topics[0] == x402_confirm.AUTHORIZATION_USED_TOPIC
            if (lo <= self.event_block <= hi
                    and topics[1].endswith(self.payer[2:])
                    and topics[2] == self.nonce):
                return [{"transactionHash": self.tx,
                         "blockNumber": hex(self.event_block)}]
            return []
        raise AssertionError(f"unexpected RPC method {method}")


def _nonce_of(payload):
    return payload.payload["authorization"]["nonce"]


# ---------------------------------------------------------------------------
# the anchor: persisted before settlement, fail-closed when unavailable
# ---------------------------------------------------------------------------

def test_safe_start_block_is_persisted_with_the_payment_identifier(
        mainnet_store, fac, monkeypatch):
    s = mainnet_store()
    _confirm_ok(monkeypatch)
    monkeypatch.setattr(x402_confirm, "current_block",
                        lambda network=None, timeout=15.0: 5_000_000)
    preq, pid = _preq(), _pid()
    payload = _with_pid(make_payload(preq), pid)
    _serve(payload, preq)
    rec = s.x402_payment_id_get(pid)
    assert rec is not None
    anchor = rec.get("recovery_from_block")
    assert isinstance(anchor, int) and 0 < anchor <= 5_000_000, (
        "a safe starting Base block must be persisted with the "
        "payment-identifier record before mainnet settlement")


def test_no_anchor_means_no_settlement_fail_closed(mainnet_store, fac,
                                                   monkeypatch):
    mainnet_store()
    monkeypatch.setattr(x402_confirm, "current_block",
                        lambda network=None, timeout=15.0: None)  # RPC down
    preq, pid = _preq(), _pid()
    payload = _with_pid(make_payload(preq), pid)
    with pytest.raises(payments.PaymentChallenge) as exc:
        payments.settle_x402(payload, preq)
    assert fac.settle_calls == [], (
        "if recovery safety cannot be anchored, the facilitator must never "
        "be contacted — no ambiguity may be created")
    assert exc.value.body.get("retryable") is True


# ---------------------------------------------------------------------------
# recovery WELL beyond the old 90k window
# ---------------------------------------------------------------------------

def test_recovery_far_beyond_90k_blocks_serves_exactly_one_settlement(
        mainnet_store, fac, monkeypatch):
    boot = mainnet_store
    s1 = boot()
    _confirm_ok(monkeypatch)
    anchor_head = 5_000_000
    monkeypatch.setattr(x402_confirm, "current_block",
                        lambda network=None, timeout=15.0: anchor_head)
    preq, pid = _preq(), _pid()
    payload = _with_pid(make_payload(preq), pid)

    # facilitator settles; the process crashes before the durable local record
    monkeypatch.setattr(
        s1, "record_x402_payment",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        payments.settle_x402(payload, preq)
    assert len(fac.settled_nonces) == 1
    tx = next(iter(fac.settled_nonces.values()))

    # ... 200,000 blocks (≈ 4.6 days on Base) pass before recovery
    s2 = boot()
    chain = FakeChain(payer=PAYER, nonce=_nonce_of(payload),
                      event_block=anchor_head + 3,
                      latest=anchor_head + 200_000, tx=tx)
    monkeypatch.setattr(x402_confirm, "_rpc_call", chain.rpc)

    settles_before = len(fac.settle_calls)
    body = _serve(payload, preq)
    assert len(fac.settled_nonces) == 1, "exactly one settlement, ever"
    assert len(fac.settle_calls) == settles_before, "no second settlement"
    assert chain.get_logs_calls, "recovery must have scanned from the anchor"
    lo_scanned = min(lo for lo, _ in chain.get_logs_calls)
    assert lo_scanned <= chain.event_block, (
        "the scan must start at (or before) the persisted anchor block — "
        "not at latest-90k")
    recs = [b for b in s2.billing_log
            if b.get("type") == "x402_payment"
            and b.get("transaction") == tx
            and b.get("status") == "settled_confirmed"]
    assert len(recs) == 1, "no double charge, independently confirmed"
    # the result is durably recoverable: an identical retry replays the bytes
    with pytest.raises(payments.CachedPaidResult) as exc:
        payments.settle_x402(payload, preq)
    assert exc.value.result_bytes == body


def test_recovery_with_unusable_rpc_still_fails_closed(mainnet_store, fac,
                                                       monkeypatch):
    boot = mainnet_store
    s1 = boot()
    _confirm_ok(monkeypatch)
    monkeypatch.setattr(x402_confirm, "current_block",
                        lambda network=None, timeout=15.0: 5_000_000)
    preq, pid = _preq(), _pid()
    payload = _with_pid(make_payload(preq), pid)
    monkeypatch.setattr(
        s1, "record_x402_payment",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        payments.settle_x402(payload, preq)

    boot()

    def rpc_down(url, method, params, timeout=15.0):
        raise RuntimeError("rpc unreachable")

    monkeypatch.setattr(x402_confirm, "_rpc_call", rpc_down)
    settles = len(fac.settle_calls)
    with pytest.raises(payments.PaymentChallenge) as exc:
        payments.settle_x402(payload, preq)
    assert exc.value.body.get("reason") == "settlement_state_unknown"
    assert len(fac.settle_calls) == settles
