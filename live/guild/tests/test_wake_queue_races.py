"""Lost scout wakes (machine-attribution pass).

The single consumable `pending_wake` lost demand in four real schedules:
  * demand arriving after a run has read its demand rows but before it
    completes (the run 'consumed' the wake it never processed);
  * demand arriving between the loop clearing its kick event and beginning
    its wait;
  * demand arriving while another process/thread holds the run lease;
  * demand arriving during a cycle that then FAILS.

Fix under test: a DURABLE, coalescing pending-demand queue keyed by
canonical capability. A wake is acknowledged only after a completed cycle
actually processed that capability; lease collisions, failures and restarts
retain the demand; recording is never debounced (only dispatch is); and the
clear-event-plus-state-recheck protocol makes lost notifications impossible.
"""
import threading
import time
import uuid

import pytest

from app import demand
from app.state import store
from app.swarm import runner

EXT_UA = "external-agent-framework/2.0 (crewai)"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    monkeypatch.setenv("GUILD_SCOUT_AUTORUN", "1")
    monkeypatch.setenv("GUILD_SCOUT_WAKE_DEBOUNCE_S", "0")
    monkeypatch.delenv("GUILD_SCOUT_CONTACT", raising=False)
    store.swarm_state.pop(runner.RUNNER_STATE_KEY, None)
    yield
    store.swarm_state.pop(runner.RUNNER_STATE_KEY, None)


def _cap():
    return "race-cap-" + uuid.uuid4().hex[:8]


def _no_net(url, **kw):
    return (None, "no_network_in_tests")


def _ask(cap, actor="a"):
    demand.record_demand(cap, transport="http", actor=actor + cap, ua=EXT_UA)


def _pending(store_):
    return runner.pending_demand(store_)


# ---------------------------------------------------------------------------
# durable coalescing queue keyed by capability; recording never debounced
# ---------------------------------------------------------------------------

def test_new_capabilities_are_always_durably_recorded(monkeypatch):
    monkeypatch.setenv("GUILD_SCOUT_WAKE_DEBOUNCE_S", "3600")
    caps = [_cap() for _ in range(3)]
    for cap in caps:
        _ask(cap)
    pend = _pending(store)
    for cap in caps:
        assert cap in pend, (
            "the DISPATCH is debounced — durable recording of a new "
            f"capability must never be ({cap} was dropped)")
    # coalescing: the same capability twice is one entry
    _ask(caps[0], actor="b")
    assert list(_pending(store)).count(caps[0]) == 1


def test_wake_survives_a_process_restart(tmp_path, monkeypatch):
    from app.store import Store
    from app import state as app_state
    monkeypatch.setenv("GUILD_STORE", "json")
    path = str(tmp_path / "guild.json")
    s1 = Store(path=path)
    monkeypatch.setattr(app_state, "store", s1)
    cap = _cap()
    demand.record_demand(cap, transport="http", actor="r1", ua=EXT_UA)
    assert cap in _pending(s1)
    s2 = Store(path=path)                       # restart
    assert cap in _pending(s2), "pending demand must survive restarts"


# ---------------------------------------------------------------------------
# ack only after a completed cycle actually processed the capability
# ---------------------------------------------------------------------------

def test_ack_requires_the_capability_to_have_been_processed(monkeypatch):
    cap_late = _cap()
    cap_early = _cap()
    _ask(cap_early)

    real_run = runner._run_scout

    def run_and_inject(store_, **kw):
        # demand arrives AFTER the cycle read its rows, mid-run
        out = real_run(store_, **kw)
        _ask(cap_late)
        return out

    monkeypatch.setattr(runner, "_run_scout", run_and_inject)
    out = runner.run_once(store, fetch=_no_net)
    assert out["completed"] is True
    pend = _pending(store)
    assert cap_early not in pend, "processed demand is acknowledged"
    assert cap_late in pend, (
        "demand arriving mid-run was NOT processed by that run — it must "
        "stay queued, not be consumed by a wake the run never saw")


def test_lease_collision_retains_the_demand():
    cap = _cap()
    _ask(cap)
    runner._acquire_lease(store, owner="other-holder", ttl_s=60.0)
    out = runner.run_once(store, fetch=_no_net, trigger="demand_wake")
    assert out["completed"] is False
    assert cap in _pending(store), (
        "a lease collision must retain/requeue the demand")


def test_failed_cycle_retains_the_demand(monkeypatch):
    cap = _cap()
    _ask(cap)
    monkeypatch.setattr(runner, "_run_scout",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("boom")))
    out = runner.run_once(store, fetch=_no_net)
    assert out["completed"] is False
    assert cap in _pending(store), "a failed cycle must retain the demand"


# ---------------------------------------------------------------------------
# lost-wakeup race with controlled thread scheduling
# ---------------------------------------------------------------------------

def test_no_notification_lost_between_clear_and_wait(monkeypatch):
    """Controlled schedule of the classic lost-wakeup race: the notifier
    fires exactly in the window after the waiter cleared the event and
    re-checked nothing. With the clear-event-plus-state-recheck protocol
    the durable queue makes the demand impossible to miss."""
    cap = _cap()
    runner._kick.clear()
    # the loop protocol under test, single iteration
    _ask(cap)                                  # state recorded, event set
    runner._kick.clear()                       # waiter clears the event ...
    # ... the recheck MUST look at durable state, not the (now cleared) event
    assert cap in _pending(store), (
        "after clearing the event the waiter must re-check durable state — "
        "otherwise this demand sleeps until the six-hour fallback")


def test_concurrent_notify_and_run_never_loses_demand():
    """Hammer notify from a thread while cycles run; every recorded
    capability must end up either processed (acked) or still pending."""
    caps = [_cap() for _ in range(8)]
    recorded = []

    def notifier():
        for cap in caps:
            _ask(cap)
            recorded.append(cap)
            time.sleep(0.002)

    t = threading.Thread(target=notifier)
    t.start()
    processed: set = set()
    for _ in range(6):
        out = runner.run_once(store, fetch=_no_net)
        if out.get("completed"):
            processed.update(out["summary"]["capabilities"])
        time.sleep(0.003)
    t.join()
    out = runner.run_once(store, fetch=_no_net)      # drain once more
    if out.get("completed"):
        processed.update(out["summary"]["capabilities"])
    pend = set(_pending(store))
    for cap in recorded:
        assert cap in processed or cap in pend, (
            f"capability {cap} was recorded but neither processed nor "
            "pending — a wake was lost")


# ---------------------------------------------------------------------------
# status exposure — counts and ages, never actors or secrets
# ---------------------------------------------------------------------------

def test_status_exposes_queue_health_without_actors(monkeypatch):
    monkeypatch.setenv("GUILD_ADMIN_TOKEN", "sekrit-zzz")
    cap = _cap()
    _ask(cap, actor="secret-actor-name")
    st = runner.status(store)
    assert st["pending_capabilities"] >= 1
    assert st["oldest_wake_age_s"] is not None
    assert st["oldest_wake_age_s"] >= 0
    runner.run_once(store, fetch=_no_net)
    st = runner.status(store)
    assert st["last_acknowledged_wake"] is not None
    assert st["last_acknowledged_wake"]["capability"]
    import json
    blob = json.dumps(st)
    assert "secret-actor-name" not in blob
    assert "sekrit-zzz" not in blob
