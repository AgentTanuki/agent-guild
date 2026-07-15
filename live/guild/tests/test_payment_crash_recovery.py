"""Payment crash recovery — the pre-mainnet corrective pass (A1).

These tests reproduce PROCESS CRASHES at the three boundaries the previous
payment-identifier implementation could not survive:

  W1  identifier reserved, crash BEFORE the facilitator settlement;
  W2  facilitator reports settlement, crash BEFORE the local settlement
      record is durable;
  W3  settlement record durable, crash BEFORE response bytes + receipt are
      finalised.

Invariants proved, for the SAME payment identifier + exact request +
identical signed payment:

  * a restart never causes a second settlement;
  * the buyer eventually receives exactly one result;
  * a completed retry returns byte-identical cached output;
  * concurrent duplicates settle at most once;
  * reuse with another payer, resource, payload or query fails closed —
    including while a recovery is possible;
  * a stale in-flight record is never permanently stuck, and is never
    deleted or blindly retried while settlement may have occurred;
  * abandoned identifiers and A2A payment tasks are garbage-collected on a
    bounded retention, WITHOUT deleting completed financial evidence.

Both persistence modes (JSON file store and SQLite backend) are exercised
with REAL store reloads from disk — a fresh Store object over the same
path, with process-local memory (in-flight registry, replay guard) cleared,
which is exactly what a process restart leaves behind.
"""
import json
import threading
import time
import uuid

import pytest

from app import payments, x402, x402_confirm
from app import state as app_state
from app.store import Store
from tests.test_x402_v2 import FakeFacilitator, make_payload, sig_header
from tests.test_payment_identifier import _with_pid, _pid

PAY_TO = "0x" + "11" * 20
PAYER = "0x" + "22" * 20


class NonceTrackingFacilitator(FakeFacilitator):
    """A facilitator that enforces EIP-3009 single-use nonces, like the real
    chain does: settling the same (payer, nonce) twice fails. Lets a test
    PROVE 'at most one settlement' rather than assume it."""

    def __init__(self, *a, settle_latency=0.0, **kw):
        super().__init__(*a, **kw)
        self.settled_nonces: dict[str, str] = {}   # nonce -> tx hash
        self.settle_latency = settle_latency
        self._lock = threading.Lock()

    def settle(self, payload, requirements):
        if self.settle_latency:
            time.sleep(self.settle_latency)
        nonce = payload.payload["authorization"]["nonce"]
        from types import SimpleNamespace
        with self._lock:
            if nonce in self.settled_nonces:
                self.settle_calls.append((payload, requirements))
                return SimpleNamespace(
                    success=False,
                    error_reason="authorization_nonce_already_used",
                    transaction="", network=requirements.network, payer=PAYER)
            resp = super().settle(payload, requirements)
            if getattr(resp, "success", False):
                self.settled_nonces[nonce] = resp.transaction
            return resp


@pytest.fixture(params=["json", "sqlite"])
def store_env(request, tmp_path, monkeypatch):
    """A DURABLE store at a real path, in each persistence mode, swapped in
    for the shared gateway store. Yields a factory that (re)loads the store
    from disk — the 'process restart'."""
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    monkeypatch.setenv("GUILD_X402_RECOVERY_LEASE_S", "0")   # tests own timing
    if request.param == "sqlite":
        monkeypatch.setenv("GUILD_STORE", "sqlite")
    else:
        monkeypatch.setenv("GUILD_STORE", "json")
    data = str(tmp_path / "guild.json")

    def boot():
        """Boot 'a process': fresh Store from the same disk path; process
        memory (in-flight pid registry + replay guard) starts empty."""
        s = Store(path=data)
        monkeypatch.setattr(app_state, "store", s)
        reset = getattr(payments, "_inflight_reset_for_process_restart", None)
        if reset is not None:      # absent on the pre-recovery implementation
            reset()
        x402.replay_guard._seen.clear()
        return s

    yield boot


@pytest.fixture
def fac(monkeypatch):
    f = NonceTrackingFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: f)
    return f


def _lookup_stub(monkeypatch, result):
    """Deterministic stand-in for the on-chain EIP-3009 nonce oracle."""
    calls = []

    def fake(payer, nonce, *, asset, network=None):
        calls.append((payer, nonce))
        return dict(result)

    monkeypatch.setattr(x402_confirm, "find_authorization_used", fake,
                        raising=False)
    return calls


def _serve(payload, preq):
    """The full paid flow at the gateway level: authorize (verify+settle),
    produce deterministic response bytes, finalize (receipt + durable
    cached result). Returns the exact bytes served."""
    auth = payments.authorize(preq, payment=payload, protocol="v2",
                              transport="http")
    assert auth.mode == "x402" and auth.settled is not None
    body = json.dumps({"capability_result": True,
                       "resource": preq.resource_url}).encode("utf-8")
    auth.settled.finalize(body)
    return body


def _preq():
    # a unique capability per test → a unique exact resource per test
    return payments.search_request("cap-" + uuid.uuid4().hex[:12])


# ---------------------------------------------------------------------------
# W1 — reserved, crash before facilitator settlement
# ---------------------------------------------------------------------------

def test_w1_crash_after_reserve_retry_settles_exactly_once(store_env, fac,
                                                           monkeypatch):
    boot = store_env
    s1 = boot()
    preq = _preq()
    pid = _pid()
    payload = _with_pid(make_payload(preq), pid)
    # crash BEFORE any facilitator contact: the reservation is durable, the
    # process dies. Reproduced by reserving through the real handler and then
    # abandoning the request.
    def _boom(*a, **k):
        raise RuntimeError("simulated crash")
    with monkeypatch.context() as m:
        m.setattr(x402, "process_payment", _boom)
        with pytest.raises(RuntimeError, match="simulated crash"):
            payments.settle_x402(payload, preq)
    assert s1.x402_payment_id_get(pid) is not None      # reservation survived
    assert fac.settle_calls == []                        # nothing settled

    # RESTART + retry (identical id, request, signed payment)
    boot()
    _lookup_stub(monkeypatch, {"used": False, "definitive": True})
    body = _serve(payload, preq)
    assert len(fac.settled_nonces) == 1                  # exactly ONE settlement
    # a completed retry returns byte-identical cached output
    with pytest.raises(payments.CachedPaidResult) as exc:
        payments.settle_x402(payload, preq)
    assert exc.value.result_bytes == body


# ---------------------------------------------------------------------------
# W2 — facilitator settled, crash before the local settlement record is durable
# ---------------------------------------------------------------------------

def test_w2_facilitator_settled_before_durable_record_never_settles_twice(
        store_env, fac, monkeypatch):
    boot = store_env
    s1 = boot()
    preq = _preq()
    pid = _pid()
    payload = _with_pid(make_payload(preq), pid)

    def _crash_record(*a, **k):
        raise RuntimeError("simulated crash before durable settlement record")

    monkeypatch.setattr(s1, "record_x402_payment", _crash_record)
    with pytest.raises(RuntimeError, match="simulated crash"):
        payments.settle_x402(payload, preq)
    # the facilitator DID settle; the local record is NOT durable
    assert len(fac.settled_nonces) == 1
    tx = next(iter(fac.settled_nonces.values()))
    assert not any(b.get("type") == "x402_payment"
                   and b.get("status") in ("settled", "settled_confirmed")
                   for b in s1.billing_log)

    # RESTART: the only truth about the settlement lives on-chain
    s2 = boot()
    calls = _lookup_stub(monkeypatch, {"used": True, "definitive": True,
                                       "transaction": tx})
    settles_before = len(fac.settle_calls)
    body = _serve(payload, preq)
    # never a second settlement — the recovered settlement is ADOPTED
    assert len(fac.settled_nonces) == 1
    assert len(fac.settle_calls) == settles_before       # facilitator untouched
    assert calls, "recovery must consult the on-chain nonce oracle"
    # the adopted settlement became durable financial evidence exactly once
    recs = [b for b in s2.billing_log
            if b.get("type") == "x402_payment" and b.get("transaction") == tx]
    assert len(recs) == 1
    # byte-identical replay from the completed record
    with pytest.raises(payments.CachedPaidResult) as exc:
        payments.settle_x402(payload, preq)
    assert exc.value.result_bytes == body


def test_w2_unknown_chain_state_fails_closed_without_retry_or_delete(
        store_env, fac, monkeypatch):
    """If settlement MAY have occurred and the chain cannot be consulted, the
    record must be neither deleted nor blindly retried: the buyer gets a
    retryable machine-readable challenge and no facilitator call happens."""
    boot = store_env
    s1 = boot()
    preq = _preq()
    pid = _pid()
    payload = _with_pid(make_payload(preq), pid)
    monkeypatch.setattr(s1, "record_x402_payment",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("simulated crash")))
    with pytest.raises(RuntimeError):
        payments.settle_x402(payload, preq)
    s2 = boot()
    _lookup_stub(monkeypatch, {"used": None, "definitive": False,
                               "reason": "rpc_unavailable"})
    settles = len(fac.settle_calls)
    with pytest.raises(payments.PaymentChallenge) as exc:
        payments.settle_x402(payload, preq)
    assert exc.value.body.get("reason") == "settlement_state_unknown"
    assert len(fac.settle_calls) == settles              # no blind retry
    rec = s2.x402_payment_id_get(pid)
    assert rec is not None                               # never deleted
    assert rec.get("status") != "completed"


# ---------------------------------------------------------------------------
# W3 — settlement durable, crash before response bytes + receipt finalised
# ---------------------------------------------------------------------------

def test_w3_settled_but_unserved_retry_serves_one_result_without_resettling(
        store_env, fac, monkeypatch):
    boot = store_env
    s1 = boot()
    preq = _preq()
    pid = _pid()
    payload = _with_pid(make_payload(preq), pid)
    # settlement completes and is durable; the process crashes BEFORE the
    # response bytes/receipt are finalised (finalize never runs).
    settled = payments.settle_x402(payload, preq)
    assert settled.record.get("ok")
    assert len(fac.settled_nonces) == 1
    assert any(b.get("type") == "x402_payment"
               and b.get("status") in ("settled", "settled_confirmed")
               for b in s1.billing_log)

    # RESTART + identical retry: the buyer must receive one result and the
    # payer must never be charged twice.
    boot()
    settles_before = len(fac.settle_calls)
    body = _serve(payload, preq)
    assert len(fac.settled_nonces) == 1                  # ONE settlement ever
    assert len(fac.settle_calls) == settles_before
    with pytest.raises(payments.CachedPaidResult) as exc:
        payments.settle_x402(payload, preq)
    assert exc.value.result_bytes == body                # byte-identical


# ---------------------------------------------------------------------------
# Fail-closed binding during recovery + concurrency + lease
# ---------------------------------------------------------------------------

def test_recovery_still_fails_closed_on_any_binding_mutation(store_env, fac,
                                                             monkeypatch):
    boot = store_env
    boot()
    preq = _preq()
    pid = _pid()
    payload = _with_pid(make_payload(preq), pid)
    settled = payments.settle_x402(payload, preq)        # W3-style: unserved
    assert settled.record.get("ok")
    boot()                                               # restart
    cap = dict(preq.query)["capability"]

    # different QUERY (same operation) → different exact resource → conflict
    other_preq = payments.search_request(cap, limit=5)
    with pytest.raises(payments.PaymentIdConflict):
        payments.settle_x402(_with_pid(make_payload(other_preq), pid),
                             other_preq)
    # different RESOURCE → conflict
    other = payments.search_request("cap-" + uuid.uuid4().hex[:8])
    with pytest.raises(payments.PaymentIdConflict):
        payments.settle_x402(_with_pid(make_payload(other), pid), other)
    # different PAYLOAD (fresh nonce) → conflict
    with pytest.raises(payments.PaymentIdConflict):
        payments.settle_x402(_with_pid(make_payload(preq), pid), preq)
    # different PAYER → conflict
    p = make_payload(preq)
    p.payload["authorization"]["from"] = "0x" + "33" * 20
    with pytest.raises(payments.PaymentIdConflict):
        payments.settle_x402(_with_pid(p, pid), preq)
    assert len(fac.settled_nonces) == 1                  # nothing new settled


def test_concurrent_duplicates_settle_at_most_once(store_env, monkeypatch):
    boot = store_env
    boot()
    fac = NonceTrackingFacilitator(settle_latency=0.15)
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    preq = _preq()
    pid = _pid()
    payload = _with_pid(make_payload(preq), pid)
    outcomes = []

    def worker():
        try:
            outcomes.append(("served", _serve(payload, preq)))
        except payments.PaymentIdConflict as e:
            outcomes.append(("conflict", e.reason))
        except payments.CachedPaidResult as e:
            outcomes.append(("cached", e.result_bytes))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(fac.settled_nonces) <= 1
    served = [o for o in outcomes if o[0] == "served"]
    assert len(served) == 1                              # exactly one result
    assert all(o[0] in ("served", "conflict", "cached") for o in outcomes)
    # afterwards, the same id + payment replays the cached bytes
    with pytest.raises(payments.CachedPaidResult) as exc:
        payments.settle_x402(payload, preq)
    assert exc.value.result_bytes == served[0][1]


def test_recovery_lease_defers_then_allows(store_env, fac, monkeypatch):
    """A record another (possibly live) worker may own is not recovered until
    the lease expires — but it IS recoverable afterwards: stale != stuck."""
    boot = store_env
    monkeypatch.setenv("GUILD_X402_RECOVERY_LEASE_S", "3600")
    s1 = boot()
    preq = _preq()
    pid = _pid()
    payload = _with_pid(make_payload(preq), pid)
    with monkeypatch.context() as m:
        m.setattr(x402, "process_payment",
                  lambda *a, **k: (_ for _ in ()).throw(
                      RuntimeError("simulated crash")))
        with pytest.raises(RuntimeError):
            payments.settle_x402(payload, preq)
    s2 = boot()
    _lookup_stub(monkeypatch, {"used": False, "definitive": True})
    # within the lease: defer (machine-readable in-flight conflict)
    with pytest.raises(payments.PaymentIdConflict) as exc:
        payments.settle_x402(payload, preq)
    assert exc.value.reason == "payment_identifier_in_flight"
    # after the lease: recoverable, settles exactly once
    rec = s2.x402_payment_id_get(pid)
    s2.x402_payment_id_transition(
        pid, rec.get("status") or "settling",
        state_changed_at=time.time() - 7200)
    body = _serve(payload, preq)
    assert len(fac.settled_nonces) == 1
    assert body


# ---------------------------------------------------------------------------
# Bounded retention / garbage collection
# ---------------------------------------------------------------------------

def test_gc_reaps_abandoned_but_never_completed_or_ambiguous(store_env, fac,
                                                             monkeypatch):
    boot = store_env
    s = boot()
    monkeypatch.setenv("GUILD_X402_PID_RETENTION_S", "100")
    monkeypatch.setenv("GUILD_X402_TASK_RETENTION_S", "100")
    old = time.time() - 1000

    # abandoned RESERVED identifier (no settlement possible) → reaped
    s.x402_payment_id_reserve("pay_" + "a" * 16, payer=PAYER.lower(),
                              request_hash="rh1", resource="r1",
                              operation="best_agent",
                              payload_fingerprint="f1")
    s.x402_payment_id_transition("pay_" + "a" * 16, "reserved",
                                 state_changed_at=old)
    # abandoned SETTLING identifier (settlement may have occurred) → kept
    s.x402_payment_id_reserve("pay_" + "b" * 16, payer=PAYER.lower(),
                              request_hash="rh2", resource="r2",
                              operation="best_agent",
                              payload_fingerprint="f2")
    s.x402_payment_id_transition("pay_" + "b" * 16, "settling",
                                 state_changed_at=old)
    # COMPLETED identifier (financial evidence) → kept forever
    s.x402_payment_id_reserve("pay_" + "c" * 16, payer=PAYER.lower(),
                              request_hash="rh3", resource="r3",
                              operation="best_agent",
                              payload_fingerprint="f3")
    s.x402_payment_id_complete("pay_" + "c" * 16, result_body="{}",
                               result_sha256="s", settle_header="h",
                               settle_extensions={},
                               settlement={"transaction": "0x1"})
    s.x402_payment_id_transition("pay_" + "c" * 16, "completed",
                                 state_changed_at=old)
    # abandoned A2A payment task → reaped; completed task → kept
    s.x402_task_create({"id": "x402task_old", "status": "payment-required",
                        "created_at_epoch": old})
    s.x402_task_create({"id": "x402task_done", "status": "payment-completed",
                        "created_at_epoch": old,
                        "transaction": "0x2"})

    summary = s.x402_gc()
    assert s.x402_payment_id_get("pay_" + "a" * 16) is None
    kept_settling = s.x402_payment_id_get("pay_" + "b" * 16)
    assert kept_settling is not None                     # never deleted
    assert kept_settling.get("abandoned") is True        # but flagged
    assert s.x402_payment_id_get("pay_" + "c" * 16) is not None
    assert s.x402_task_get("x402task_old") is None
    assert s.x402_task_get("x402task_done") is not None
    assert summary["payment_ids_reaped"] == 1
    assert summary["tasks_reaped"] == 1

    # GC durability: a reload sees the same post-GC state
    s2 = boot()
    assert s2.x402_payment_id_get("pay_" + "a" * 16) is None
    assert s2.x402_payment_id_get("pay_" + "c" * 16) is not None
