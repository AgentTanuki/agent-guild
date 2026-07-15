"""The autonomous scout runner (pre-mainnet swarm completion pass).

Defect reproduced: run_scout() existed but NOTHING in production invoked it —
"autonomy" required a human or an external button. The runner must:

  * be explicitly enabled (GUILD_SCOUT_AUTORUN=1 in Render), OFF by default;
  * persist a lease so overlapping runs are impossible (across restarts too);
  * apply jitter/backoff and a bounded per-run deadline;
  * keep outbound contact OFF (independent of the runner);
  * publish /swarm/status — enabled state, last start/completion, adapter
    results, discovered/refreshed counts, last error — and no secrets;
  * count a zero-demand cycle as a legitimate COMPLETED cycle.
"""
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app.state import store
from app.swarm import runner, scout

PAY_TO = "0x" + "11" * 20
EXT_UA = "external-agent-framework/2.0 (langchain)"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    monkeypatch.delenv("GUILD_SCOUT_AUTORUN", raising=False)
    monkeypatch.delenv("GUILD_SCOUT_CONTACT", raising=False)
    store.swarm_state.pop(runner.RUNNER_STATE_KEY, None)   # clean lease/state
    yield
    store.swarm_state.pop(runner.RUNNER_STATE_KEY, None)


def _no_net(url, **kw):
    return (None, "no_network_in_tests")


def test_runner_is_off_by_default_and_needs_explicit_enable(monkeypatch):
    assert runner.enabled() is False
    monkeypatch.setenv("GUILD_SCOUT_AUTORUN", "1")
    assert runner.enabled() is True
    monkeypatch.setenv("GUILD_SCOUT_AUTORUN", "0")
    assert runner.enabled() is False


def test_zero_demand_cycle_completes_and_is_recorded(monkeypatch):
    monkeypatch.setenv("GUILD_SCOUT_AUTORUN", "1")
    out = runner.run_once(store, fetch=_no_net)
    assert out["completed"] is True
    st = store.swarm_state["scout_runner"]
    assert st["last_started_at"] and st["last_completed_at"]
    assert st["last_error"] is None
    assert st["runs_completed"] >= 1
    assert out["zero_demand"] in (True, False)


def test_lease_prevents_overlapping_runs(monkeypatch):
    monkeypatch.setenv("GUILD_SCOUT_AUTORUN", "1")
    # another live holder owns the lease
    runner._acquire_lease(store, owner="other-process",
                          ttl_s=60.0, now=time.time())
    out = runner.run_once(store, fetch=_no_net)
    assert out["completed"] is False
    assert "lease" in out["reason"]


def test_expired_lease_is_recoverable_after_restart(monkeypatch):
    monkeypatch.setenv("GUILD_SCOUT_AUTORUN", "1")
    # a crashed process left a stale lease behind
    runner._acquire_lease(store, owner="dead-process",
                          ttl_s=0.0, now=time.time() - 10)
    out = runner.run_once(store, fetch=_no_net)
    assert out["completed"] is True


def test_runner_records_errors_and_backs_off(monkeypatch):
    monkeypatch.setenv("GUILD_SCOUT_AUTORUN", "1")

    def _boom(store_, **kw):
        raise RuntimeError("adapter meltdown")

    monkeypatch.setattr(runner, "_run_scout", _boom)
    out = runner.run_once(store, fetch=_no_net)
    assert out["completed"] is False
    st = store.swarm_state["scout_runner"]
    assert "adapter meltdown" in (st["last_error"] or "")
    assert st["consecutive_failures"] >= 1
    # backoff grows with consecutive failures, bounded by the interval
    d1 = runner.next_delay_s(failures=1)
    d3 = runner.next_delay_s(failures=3)
    assert 0 < d1 <= d3 <= runner.interval_s()


def test_jitter_spreads_the_schedule():
    delays = {round(runner.next_delay_s(failures=0), 3) for _ in range(20)}
    assert len(delays) > 1, "the schedule must be jittered"
    for d in delays:
        assert 0 < d <= runner.interval_s() * 1.5


def test_contact_stays_off_even_when_runner_is_enabled(monkeypatch):
    monkeypatch.setenv("GUILD_SCOUT_AUTORUN", "1")
    assert scout.contact_enabled() is False
    runner.run_once(store, fetch=_no_net)
    assert scout.contact_enabled() is False


def test_swarm_status_endpoint_exposes_state_without_secrets(monkeypatch):
    from app.main import app
    monkeypatch.setenv("GUILD_SCOUT_AUTORUN", "1")
    monkeypatch.setenv("GUILD_ADMIN_TOKEN", "sekrit-admin-token")
    monkeypatch.setenv("GUILD_FIRST_PARTY_TOKEN", "sekrit-fp-token")
    cap = "runner-cap-" + uuid.uuid4().hex[:6]
    with TestClient(app) as client:
        client.get(f"/check?capability={cap}", headers={"User-Agent": EXT_UA})
        runner.run_once(store, fetch=_no_net)
        r = client.get("/swarm/status")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert body["contact_enabled"] is False
        assert body["last_started_at"] and body["last_completed_at"]
        assert "adapters" in body["last_run"]
        assert {"discovered", "refreshed"} <= set(body["last_run"])
        assert body["last_error"] is None
        text = r.text
        assert "sekrit-admin-token" not in text
        assert "sekrit-fp-token" not in text


def test_release_gate_verifies_a_completed_production_scout_cycle(monkeypatch):
    """The gate must WAIT for one completed scout cycle belonging to the
    expected release — a zero-demand cycle counts — and fail when the
    runner is disabled or the cycle errors. (Full race/identity coverage:
    tests/test_release_gate_race.py.)"""
    import importlib.util
    import pathlib
    path = (pathlib.Path(__file__).resolve().parents[2]
            / "scripts" / "release_gate.py")
    spec = importlib.util.spec_from_file_location("release_gate", path)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)

    sha = "d" * 40
    seq = [
        {"enabled": True, "last_completed_at": None,
         "last_started_at": "2026-07-15T00:00:01+00:00", "last_error": None},
        {"enabled": True, "last_completed_at": "2026-07-15T00:00:05+00:00",
         "last_started_at": "2026-07-15T00:00:01+00:00", "last_error": None,
         "last_run": {"zero_demand": True, "capabilities": [],
                      "release_sha": sha,
                      "discovered": 0, "refreshed": 0, "adapters": {}}},
    ]
    calls = {"n": 0}

    def fake_get(host, path_, timeout=45.0, api_key=""):
        assert path_ == "/swarm/status"
        out = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return out

    monkeypatch.setattr(gate, "_get", fake_get)
    fails = gate.check_swarm_cycle("https://prod.example", expected_sha=sha,
                                   timeout_s=5.0, interval_s=0.0)
    assert fails == []              # zero-demand cycle is a legitimate PASS

    # disabled runner → the gate fails with a clear reason
    monkeypatch.setattr(gate, "_get",
                        lambda *a, **k: {"enabled": False})
    fails = gate.check_swarm_cycle("https://prod.example", expected_sha=sha,
                                   timeout_s=0.5, interval_s=0.0)
    assert fails and "enabled" in fails[0]
