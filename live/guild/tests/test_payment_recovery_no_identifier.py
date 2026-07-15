"""Every mainnet payment must be crash-recoverable — with or WITHOUT the
optional payment-identifier extension (machine-autonomy corrective pass).

Defect reproduced: a valid, official-client-compatible mainnet payload that
carries NO payment-identifier extension reached the facilitator with no
persisted recovery record and no Base anchor. A crash in the ambiguous
window then left the buyer's money in a state only the OPTIONAL extension
could have recovered. Safety must never depend on an optional extension.

Fix under test: a deterministic INTERNAL recovery identity is derived from
the bound signed authorization + request properties (network, payer, nonce,
amount, asset, recipient, resource, method) for every mainnet settlement;
reservation + payment identity + safe starting block are persisted BEFORE
the facilitator is contacted; the external extension remains caller-visible
idempotency when supplied; fail closed pre-facilitator when the durable
record or anchor cannot be created.
"""
import json
import threading
import uuid

import pytest

from app import payments, x402, x402_confirm
from app import state as app_state
from app.store import Store
from tests.test_x402_v2 import make_payload
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


def _anchor(monkeypatch, block=5_000_000):
    monkeypatch.setattr(x402_confirm, "current_block",
                        lambda network=None, timeout=15.0: block)


def _confirm_ok(monkeypatch):
    monkeypatch.setattr(x402_confirm, "_get_receipt",
                        lambda tx, timeout=15.0: _receipt())


def _preq():
    return payments.search_request("bare-" + uuid.uuid4().hex[:12])


def _serve(payload, preq):
    auth = payments.authorize(preq, payment=payload, protocol="v2",
                              transport="http")
    assert auth.mode == "x402" and auth.settled is not None
    body = json.dumps({"ok": True, "resource": preq.resource_url}).encode()
    auth.settled.finalize(body)
    return body


# ---------------------------------------------------------------------------
# durable pre-facilitator recovery record — for BARE payloads
# ---------------------------------------------------------------------------

def test_bare_mainnet_payment_has_durable_recovery_record_before_facilitator(
        mainnet_store, monkeypatch):
    s = mainnet_store()
    _anchor(monkeypatch)
    _confirm_ok(monkeypatch)
    seen: dict = {}

    class RecordingFacilitator(NonceTrackingFacilitator):
        def settle(self, payload, requirements):
            # snapshot durable state at the exact moment of facilitator
            # contact — the crash-recovery contract must already hold HERE.
            recs = [dict(r) for r in s.x402_payment_ids.values()]
            seen["at_settle"] = recs
            return super().settle(payload, requirements)

    monkeypatch.setattr(x402, "_facilitator",
                        lambda: RecordingFacilitator(network=MAINNET))
    preq = _preq()
    payload = make_payload(preq)          # NO payment-identifier extension
    _serve(payload, preq)
    recs = seen.get("at_settle") or []
    settling = [r for r in recs if r.get("status") == "settling"]
    assert settling, (
        "a bare (no payment-identifier) mainnet payment reached the "
        "facilitator WITHOUT a persisted recovery record — safety must not "
        f"depend on the optional extension (records at settle: {recs})")
    assert isinstance(settling[0].get("recovery_from_block"), int), (
        "the safe starting Base block must be persisted with the record "
        "before facilitator contact")
    assert settling[0].get("payer") == PAYER.lower()


def test_internal_recovery_identity_is_deterministic_and_bound():
    preq = _preq()
    p1 = make_payload(preq)
    rid1 = payments.internal_recovery_id(p1, preq)
    rid2 = payments.internal_recovery_id(p1, preq)
    assert rid1 == rid2, "same signed authorization + request → same id"
    # a different nonce (different payment) → different id
    p2 = make_payload(preq, nonce="0x" + "ab" * 32)
    assert payments.internal_recovery_id(p2, preq) != rid1
    # a different resource → different id
    other = payments.search_request("bare-other-" + uuid.uuid4().hex[:8])
    assert payments.internal_recovery_id(p1, other) != rid1


def test_bare_anchor_unavailable_fails_closed_before_facilitator(
        mainnet_store, fac, monkeypatch):
    mainnet_store()
    monkeypatch.setattr(x402_confirm, "current_block",
                        lambda network=None, timeout=15.0: None)
    preq = _preq()
    with pytest.raises(payments.PaymentChallenge) as exc:
        payments.settle_x402(make_payload(preq), preq)
    assert fac.settle_calls == []
    assert exc.value.body.get("retryable") is True


# ---------------------------------------------------------------------------
# crashes without the extension: before and after facilitator response
# ---------------------------------------------------------------------------

def test_bare_crash_before_facilitator_then_retry_settles_exactly_once(
        mainnet_store, fac, monkeypatch):
    boot = mainnet_store
    boot()
    _anchor(monkeypatch)
    _confirm_ok(monkeypatch)
    preq = _preq()
    payload = make_payload(preq)
    with monkeypatch.context() as m:
        m.setattr(x402, "process_payment",
                  lambda *a, **k: (_ for _ in ()).throw(
                      RuntimeError("simulated crash")))
        with pytest.raises(RuntimeError, match="simulated crash"):
            payments.settle_x402(payload, preq)
    assert fac.settle_calls == []
    # RESTART + identical bare retry
    boot()
    _lookup = {"used": False, "definitive": True}
    monkeypatch.setattr(x402_confirm, "find_authorization_used",
                        lambda *a, **k: dict(_lookup), raising=False)
    body = _serve(payload, preq)
    assert len(fac.settled_nonces) == 1
    assert body


def test_bare_crash_after_facilitator_recovers_200k_blocks_later(
        mainnet_store, fac, monkeypatch):
    from tests.test_payment_recovery_deep_history import FakeChain
    boot = mainnet_store
    s1 = boot()
    anchor_head = 5_000_000
    _anchor(monkeypatch, anchor_head)
    _confirm_ok(monkeypatch)
    preq = _preq()
    payload = make_payload(preq)          # NO extension
    monkeypatch.setattr(
        s1, "record_x402_payment",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        payments.settle_x402(payload, preq)
    assert len(fac.settled_nonces) == 1
    tx = next(iter(fac.settled_nonces.values()))

    s2 = boot()
    nonce = payload.payload["authorization"]["nonce"]
    chain = FakeChain(payer=PAYER, nonce=nonce,
                      event_block=anchor_head + 3,
                      latest=anchor_head + 200_000, tx=tx)
    monkeypatch.setattr(x402_confirm, "_rpc_call", chain.rpc)
    settles_before = len(fac.settle_calls)
    body = _serve(payload, preq)
    assert len(fac.settled_nonces) == 1, "exactly one settlement, ever"
    assert len(fac.settle_calls) == settles_before
    recs = [b for b in s2.billing_log
            if b.get("type") == "x402_payment"
            and b.get("transaction") == tx
            and b.get("status") == "settled_confirmed"]
    assert len(recs) == 1, "no double charge"
    # replay of the identical bare payload → the cached result, no re-settle
    with pytest.raises(payments.CachedPaidResult) as exc:
        payments.settle_x402(payload, preq)
    assert exc.value.result_bytes == body
    assert len(fac.settled_nonces) == 1


def test_bare_concurrent_retries_settle_at_most_once(mainnet_store,
                                                     monkeypatch):
    s = mainnet_store()
    _anchor(monkeypatch)
    _confirm_ok(monkeypatch)
    fac = NonceTrackingFacilitator(network=MAINNET, settle_latency=0.05)
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    preq = _preq()
    payload = make_payload(preq)
    outcomes: list[str] = []

    def attempt():
        try:
            settled = payments.settle_x402(payload, preq)
            settled.finalize(b'{"ok": true}')
            outcomes.append("served")
        except payments.CachedPaidResult:
            outcomes.append("cached")
        except (payments.PaymentIdConflict, payments.PaymentChallenge,
                x402.PaymentBindingError) as e:
            outcomes.append(type(e).__name__)

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(fac.settled_nonces) <= 1, "at most ONE settlement"
    assert outcomes.count("served") <= 1


# ---------------------------------------------------------------------------
# the external extension stays caller-visible idempotency
# ---------------------------------------------------------------------------

def test_external_identifier_still_provides_caller_visible_idempotency(
        mainnet_store, fac, monkeypatch):
    mainnet_store()
    _anchor(monkeypatch)
    _confirm_ok(monkeypatch)
    preq = _preq()
    pid = _pid()
    payload = _with_pid(make_payload(preq), pid)
    body = _serve(payload, preq)
    with pytest.raises(payments.CachedPaidResult) as exc:
        payments.settle_x402(payload, preq)
    assert exc.value.result_bytes == body
    assert exc.value.record["payment_id"] == pid   # the CALLER's identifier
    assert len(fac.settled_nonces) == 1
