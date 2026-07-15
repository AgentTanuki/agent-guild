"""Truthful scout status + demand-driven scheduling (corrective pass).

Defects reproduced:
  * a ZERO-DEMAND cycle reported every adapter as ok/ran — but no adapter
    was ever invoked. Status must say `not_run` / skipped: no_demand.
  * newly counted genuine external unmet demand did nothing until the next
    six-hour tick. A debounced, rate-limited wake signal must schedule a
    prompt cycle; the interval stays as fallback; the overlap lease still
    prevents concurrent runs; repeated demand can never flood registries.
  * /swarm/status did not say WHY the last cycle ran, which release it ran
    under, or how to read a zero-demand cycle.
"""
import uuid

import pytest

from app import demand
from app.state import store
from app.swarm import runner, scout

EXT_UA = "external-agent-framework/2.0 (crewai)"
SHA = "c" * 40


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    monkeypatch.setenv("GUILD_SCOUT_AUTORUN", "1")
    monkeypatch.delenv("GUILD_SCOUT_CONTACT", raising=False)
    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA)
    store.swarm_state.pop(runner.RUNNER_STATE_KEY, None)
    yield
    store.swarm_state.pop(runner.RUNNER_STATE_KEY, None)


def _no_net(url, **kw):
    return (None, "no_network_in_tests")


def _cap():
    return "wake-cap-" + uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# zero-demand truthfulness
# ---------------------------------------------------------------------------

def test_zero_demand_cycle_reports_adapters_not_run(monkeypatch):
    # a genuinely zero-demand feed (other tests in this process may have
    # recorded demand into the shared store)
    monkeypatch.setattr(store, "demand_feed_entries", lambda: [])
    summary = scout.run_scout(store, fetch=_no_net)
    assert summary["capabilities"] == []
    for name, res in summary["adapters"].items():
        assert res.get("status") in ("not_run", "skipped"), (
            f"adapter {name} was never invoked in a zero-demand cycle but "
            f"reported {res!r} — status must be not_run/skipped")
        assert res.get("reason") == "no_demand"
        assert "ok" not in res or res.get("ok") is not True, (
            "an adapter that never ran must not claim ok")


def test_adapters_that_actually_ran_report_ran():
    cap = _cap()
    demand.record_demand(cap, transport="http", actor="a-status", ua=EXT_UA)
    summary = scout.run_scout(store, fetch=_no_net)
    assert cap in summary["capabilities"]
    assert summary["adapters"]["a2a_registry"].get("status") == "ran"


def test_status_classifies_the_last_cycle(monkeypatch):
    runner.run_once(store, fetch=_no_net)
    st = runner.status(store)
    run = st["last_run"]
    assert run["classification"] in ("zero_demand", "demand_cycle")
    assert st["release_sha"] == SHA
    assert "wake" in st          # wake block present (None or details)


# ---------------------------------------------------------------------------
# demand wake: debounced, rate-limited, lease-safe
# ---------------------------------------------------------------------------

def test_new_genuine_unmet_demand_requests_a_wake():
    cap = _cap()
    ctx = demand.record_demand(cap, transport="http", actor="a-wake",
                               ua=EXT_UA)
    assert ctx["counted"] is True
    pend = runner.pending_demand(store)
    canon = cap.lower()
    assert canon in pend, ("newly counted genuine external unmet demand "
                           "must be durably queued")
    assert pend[canon]["reason"] == "genuine_unmet_demand"


def test_non_genuine_or_first_party_demand_never_wakes():
    for kwargs in ({"ua": "curl/8.5.0"},
                   {"ua": EXT_UA, "first_party": True},
                   {"ua": "Glama-Bot/2.0 (+crawler)"}):
        store.swarm_state.pop(runner.RUNNER_STATE_KEY, None)
        demand.record_demand(_cap(), transport="http",
                             actor="a-" + uuid.uuid4().hex[:6], **kwargs)
        assert not runner.pending_demand(store), (
            f"non-genuine demand ({kwargs}) must not wake the scout")


def test_wake_is_debounced_and_rate_limited(monkeypatch):
    monkeypatch.setenv("GUILD_SCOUT_WAKE_DEBOUNCE_S", "3600")
    caps = [_cap() for _ in range(5)]
    for i, cap in enumerate(caps):
        demand.record_demand(cap, transport="http", actor=f"flood-{i}",
                             ua=EXT_UA)
    st = store.swarm_state.get(runner.RUNNER_STATE_KEY) or {}
    # DISPATCH is debounced (≤1 within the window) but every capability is
    # still durably RECORDED — recording is never debounced.
    assert st.get("wakes_requested", 0) <= 1, (
        "repeated demand inside the debounce window must collapse dispatch "
        "into at most one wake — never a registry flood")
    assert len(runner.pending_demand(store)) == 5
    # one run drains the queue; the lease still guards overlap
    out = runner.run_once(store, fetch=_no_net, trigger="demand_wake")
    assert out["completed"] is True
    assert runner.pending_demand(store) == {}
    assert st["last_run"]["trigger"] == "demand_wake"


def test_wake_never_produces_concurrent_runs():
    # a live holder owns the lease; a demand wake must not bypass it
    runner._acquire_lease(store, owner="other", ttl_s=60.0)
    demand.record_demand(_cap(), transport="http", actor="a-lease",
                         ua=EXT_UA)
    out = runner.run_once(store, fetch=_no_net, trigger="demand_wake")
    assert out["completed"] is False
    assert "lease" in out["reason"]


def test_interval_fallback_is_preserved():
    assert runner.interval_s() >= 3600.0      # six-hour default fallback
    d = runner.next_delay_s(failures=0)
    assert 0 < d <= runner.interval_s() * 1.5


def test_status_exposes_wake_without_secrets(monkeypatch):
    monkeypatch.setenv("GUILD_ADMIN_TOKEN", "sekrit-token-x")
    cap = _cap()
    demand.record_demand(cap, transport="http", actor="a-status2", ua=EXT_UA)
    runner.run_once(store, fetch=_no_net, trigger="demand_wake")
    st = runner.status(store)
    assert st["last_run"]["trigger"] == "demand_wake"
    assert st["wake"] is None or "sekrit" not in str(st["wake"])
    import json
    assert "sekrit-token-x" not in json.dumps(st)
