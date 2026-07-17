"""Deadline-aware, persisted wake scheduling (machine-integrity correction).

The defect this closes: notify_demand() CALCULATED a debounced `dispatch`
decision but then called `_kick.set()` unconditionally — so the loop woke and
ran on EVERY notification, and (worse) re-ran back-to-back while the queue was
non-empty. The debounce existed only as an unused counter.

Required properties (all under a FAKE clock — no sleeps):
  * recording into the durable queue is NEVER debounced;
  * no more than ONE demand-triggered scout run may BEGIN inside
    GUILD_SCOUT_WAKE_DEBOUNCE_S;
  * queued capabilities survive the delay, a failed run, a lease collision
    and a restart;
  * once the debounce expires, pending work is processed WITHOUT waiting for
    the six-hour interval;
  * demand arriving during a run is retained;
  * the interval remains a fallback.
"""
import threading
import uuid

import pytest

from app import demand
from app.state import store
from app.swarm import runner

EXT_UA = "external-agent-framework/2.0 (crewai)"
SHA = "d" * 40
T0 = 1_700_000_000.0


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GUILD_SCOUT_AUTORUN", "1")
    monkeypatch.setenv("GUILD_SCOUT_WAKE_DEBOUNCE_S", "600")
    monkeypatch.delenv("GUILD_SCOUT_CONTACT", raising=False)
    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA)
    store.swarm_state.pop(runner.RUNNER_STATE_KEY, None)
    events_before = len(store.events)
    dedupe_before = dict(store.demand_dedupe)
    yield
    store.swarm_state.pop(runner.RUNNER_STATE_KEY, None)
    # this module floods demand rows on purpose — leave no residue that
    # could crowd other tests' capabilities out of the scout's per-run cap
    del store.events[events_before:]
    store.demand_dedupe.clear()
    store.demand_dedupe.update(dedupe_before)


@pytest.fixture(autouse=True)
def _isolated_feed(monkeypatch):
    """Scope scout cycles to THIS module's capabilities: the shared store
    accumulates unmet demand from every other module in the process, and
    the scout bounds capabilities per run — this module tests SCHEDULING,
    not global feed ordering."""
    orig = store.demand_feed_entries
    monkeypatch.setattr(
        store, "demand_feed_entries",
        lambda: [r for r in orig() if r["capability"].startswith("sched-")])


@pytest.fixture
def clock(monkeypatch):
    class _Clock:
        t = T0
    monkeypatch.setattr(runner, "_now", lambda: _Clock.t)
    return _Clock


def _no_net(url, **kw):
    return (None, "no_network_in_tests")


def _cap():
    return "sched-" + uuid.uuid4().hex[:8]


def _ask(cap, actor=None):
    return demand.record_demand(cap, transport="http",
                                actor=actor or ("a-" + uuid.uuid4().hex[:6]),
                                ua=EXT_UA)


# ---------------------------------------------------------------------------
# the debounce is enforced where it matters: at RUN START
# ---------------------------------------------------------------------------

def test_at_most_one_demand_run_begins_inside_the_window(clock):
    for _ in range(5):
        _ask(_cap())
    assert len(runner.pending_demand(store)) == 5   # recording not debounced
    out1 = runner.run_once(store, fetch=_no_net, trigger="demand_wake")
    assert out1["completed"] is True
    assert runner.pending_demand(store) == {}

    # new demand inside the window: durably queued, but a second
    # demand-triggered run may NOT begin.
    clock.t = T0 + 30
    _ask(_cap())
    assert len(runner.pending_demand(store)) == 1
    out2 = runner.run_once(store, fetch=_no_net, trigger="demand_wake")
    assert out2["completed"] is False
    assert out2["reason"] == "wake_debounced"
    assert len(runner.pending_demand(store)) == 1, (
        "a debounced dispatch must retain the queued capability")

    # once the window expires the pending work runs — no six-hour wait.
    clock.t = T0 + 601
    out3 = runner.run_once(store, fetch=_no_net, trigger="demand_wake")
    assert out3["completed"] is True
    assert runner.pending_demand(store) == {}


def test_notify_demand_never_unconditionally_requests_dispatch(clock):
    _ask(_cap())
    runner.run_once(store, fetch=_no_net, trigger="demand_wake")
    st = store.swarm_state[runner.RUNNER_STATE_KEY]
    claimed = st["wakes_requested"]
    clock.t = T0 + 10
    for _ in range(20):
        _ask(_cap())
    assert st["wakes_requested"] == claimed, (
        "notifications inside the window must not claim dispatch slots — "
        "the old code kicked the loop unconditionally")
    assert runner.dispatch_due(store) is False


def test_dispatch_deadline_is_persisted_and_before_the_interval(clock):
    _ask(_cap())
    runner.run_once(store, fetch=_no_net, trigger="demand_wake")
    clock.t = T0 + 5
    _ask(_cap())
    deadline = runner.next_dispatch_at(store)
    assert deadline == pytest.approx(T0 + 600.0), (
        "pending work must be scheduled for the END of the debounce window, "
        "not the six-hour interval")
    assert deadline - clock.t < runner.interval_s()
    # the deadline derives from PERSISTED state (survives a restart)
    st = store.swarm_state[runner.RUNNER_STATE_KEY]
    assert float(st["last_wake_at"]) == pytest.approx(T0)


def test_debounced_attempt_is_not_a_failure(clock):
    _ask(_cap())
    runner.run_once(store, fetch=_no_net, trigger="demand_wake")
    clock.t = T0 + 1
    _ask(_cap())
    runner.run_once(store, fetch=_no_net, trigger="demand_wake")
    st = store.swarm_state[runner.RUNNER_STATE_KEY]
    assert st["consecutive_failures"] == 0
    assert st["last_error"] is None


def test_queue_survives_failure_lease_and_restart(clock, monkeypatch):
    cap = _cap()
    _ask(cap)
    canon = cap.lower()

    # failed run: capability retained
    def _boom(store_, **kw):
        raise RuntimeError("adapter exploded")
    monkeypatch.setattr(runner, "_run_scout", _boom)
    out = runner.run_once(store, fetch=_no_net, trigger="demand_wake")
    assert out["completed"] is False
    assert canon in runner.pending_demand(store)
    monkeypatch.undo()

    # lease collision: capability retained
    runner._acquire_lease(store, owner="other", ttl_s=60.0,
                          now=runner._now())
    out = runner.run_once(store, fetch=_no_net, trigger="demand_wake")
    assert out["completed"] is False
    assert canon in runner.pending_demand(store)
    runner._release_lease(store, "other")

    # "restart": a fresh read of persisted state still sees the capability
    assert canon in runner.pending_demand(store)


def test_interval_runs_remain_a_fallback_and_drain_the_queue(clock):
    cap = _cap()
    _ask(cap)
    runner.run_once(store, fetch=_no_net, trigger="demand_wake")
    clock.t = T0 + 5
    _ask(_cap())
    # inside the debounce window an INTERVAL run (the fallback) may still
    # process pending work — the debounce constrains demand-triggered runs.
    out = runner.run_once(store, fetch=_no_net, trigger="interval")
    assert out["completed"] is True
    assert runner.pending_demand(store) == {}


# ---------------------------------------------------------------------------
# flood: many actors × many capabilities, fake clock — bounded dispatches,
# eventual drainage
# ---------------------------------------------------------------------------

def test_flood_bounded_dispatches_and_eventual_drainage(clock):
    debounce = runner.wake_debounce_s()
    caps = [_cap() for _ in range(12)]
    demand_runs = 0
    horizon = 3 * debounce
    step = 60.0
    i = 0
    t = T0
    while t < T0 + horizon:
        clock.t = t
        # several actors keep asking for (new and repeated) capabilities
        for a in range(3):
            _ask(caps[(i + a) % len(caps)], actor=f"flood-{a}")
        i += 1
        if runner.pending_demand(store) and runner.dispatch_due(store):
            out = runner.run_once(store, fetch=_no_net,
                                  trigger="demand_wake")
            if out.get("completed"):
                demand_runs += 1
        t += step
    windows = horizon / debounce
    assert demand_runs <= windows + 1, (
        f"{demand_runs} demand-triggered runs in {windows} debounce windows "
        "— the scheduler must bound dispatches to one per window")
    assert demand_runs >= 1
    # drainage: advance window by window — the queue must empty (the scout
    # bounds capabilities per run, so draining may take several windows)
    for _ in range(6):
        if not runner.pending_demand(store):
            break
        clock.t += debounce + 1
        out = runner.run_once(store, fetch=_no_net, trigger="demand_wake")
        assert out["completed"] is True
    assert runner.pending_demand(store) == {}


def test_concurrent_notifiers_never_lose_demand_under_fake_clock(clock):
    caps = [_cap() for _ in range(10)]
    errs = []

    def notifier(chunk):
        try:
            for cap in chunk:
                runner.notify_demand(store, cap)
        except Exception as e:      # noqa: BLE001
            errs.append(e)

    threads = [threading.Thread(target=notifier, args=(caps[i::2],))
               for i in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errs
    pend = runner.pending_demand(store)
    for cap in caps:
        assert cap.lower() in pend, f"{cap} was lost"


# ---------------------------------------------------------------------------
# the loop planner: deadline-aware sleep, no busy-rerun
# ---------------------------------------------------------------------------

def test_planner_sleeps_until_deadline_never_busy_loops(clock):
    _ask(_cap())
    runner.run_once(store, fetch=_no_net, trigger="demand_wake")
    clock.t = T0 + 10
    _ask(_cap())
    trigger, wait = runner._plan(store, next_interval_at=T0 + 21_600,
                                 now=clock.t)
    assert trigger is None
    assert wait == pytest.approx(590.0), (
        "with pending demand inside the window the loop must sleep exactly "
        "to the debounce deadline")
    clock.t = T0 + 601
    trigger, wait = runner._plan(store, next_interval_at=T0 + 21_600,
                                 now=clock.t)
    assert trigger == "demand_wake"


def test_planner_falls_back_to_interval(clock):
    trigger, wait = runner._plan(store, next_interval_at=T0 - 1, now=T0)
    assert trigger == "interval"
    trigger, wait = runner._plan(store, next_interval_at=T0 + 100, now=T0)
    assert trigger is None
    assert wait == pytest.approx(100.0)
