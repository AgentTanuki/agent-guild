"""Release-gate certification race (machine-autonomy corrective pass).

Defect reproduced: the gate's production_scout_cycle check required the
cycle to complete AFTER the gate process started (`completed_after` = the
gate's own start time). In reality production restarts on deploy, the
runner's first jittered cycle completes within ~30s, and the GitHub gate
job often starts minutes later — so a legitimately completed cycle for the
EXACT deployed release was rejected and the gate red-flagged healthy
releases (and, worse, a cycle left over from the PREVIOUS release could
have satisfied a timing-based check).

Fix under test: every persisted cycle carries the RELEASE IDENTITY it ran
under (RENDER_GIT_COMMIT / GUILD_GIT_SHA + process start). The gate accepts
a successfully completed cycle IFF it belongs to the exact expected SHA —
regardless of when the gate process started — and rejects cycles from any
other release, disabled runners, errored cycles and enabled contact.
"""
import importlib.util
import pathlib
import time

import pytest

from app.state import store
from app.swarm import runner

DEPLOYED_SHA = "a" * 40
STALE_SHA = "b" * 40


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GUILD_SCOUT_AUTORUN", "1")
    monkeypatch.delenv("GUILD_SCOUT_CONTACT", raising=False)
    monkeypatch.setenv("RENDER_GIT_COMMIT", DEPLOYED_SHA)
    store.swarm_state.pop(runner.RUNNER_STATE_KEY, None)
    yield
    store.swarm_state.pop(runner.RUNNER_STATE_KEY, None)


def _gate():
    path = (pathlib.Path(__file__).resolve().parents[2]
            / "scripts" / "release_gate.py")
    spec = importlib.util.spec_from_file_location("release_gate", path)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return gate


def _no_net(url, **kw):
    return (None, "no_network_in_tests")


# ---------------------------------------------------------------------------
# the runner stamps release identity on every persisted cycle
# ---------------------------------------------------------------------------

def test_every_persisted_cycle_carries_the_release_identity():
    out = runner.run_once(store, fetch=_no_net)
    assert out["completed"] is True
    st = store.swarm_state[runner.RUNNER_STATE_KEY]
    run = st["last_run"]
    assert run["release_sha"] == DEPLOYED_SHA, (
        "every persisted last_run must carry the exact release identity "
        "it executed under")
    assert run.get("process_started_at"), (
        "the cycle must carry the deployment/process start identity")


def test_swarm_status_exposes_release_identity():
    runner.run_once(store, fetch=_no_net)
    status = runner.status(store)
    assert status["release_sha"] == DEPLOYED_SHA
    assert (status.get("last_run") or {}).get("release_sha") == DEPLOYED_SHA


# ---------------------------------------------------------------------------
# the gate: identity-based, not timing-based
# ---------------------------------------------------------------------------

def _status_doc(sha=DEPLOYED_SHA, *, completed=True, error=None,
                contact=False, enabled=True, zero_demand=True):
    return {
        "enabled": enabled,
        "contact_enabled": contact,
        "release_sha": sha,
        "last_started_at": "2026-07-15T00:00:01+00:00",
        "last_completed_at": ("2026-07-15T00:00:05+00:00" if completed
                              else None),
        "last_error": error,
        "last_run": ({"release_sha": sha, "zero_demand": zero_demand,
                      "capabilities": [], "discovered": 0, "refreshed": 0,
                      "adapters": {},
                      "process_started_at": "2026-07-15T00:00:00+00:00"}
                     if completed else None),
    }


def test_cycle_completed_before_gate_started_on_expected_sha_passes(
        monkeypatch):
    """THE race: production completed its post-deploy cycle minutes before
    the gate process even started. Identity match ⇒ pass."""
    gate = _gate()
    monkeypatch.setattr(gate, "_get",
                        lambda *a, **k: _status_doc(sha=DEPLOYED_SHA))
    fails = gate.check_swarm_cycle("https://prod.example",
                                   expected_sha=DEPLOYED_SHA,
                                   timeout_s=1.0, interval_s=0.0)
    assert fails == [], (
        "a completed cycle belonging to the exact deployed SHA must pass "
        f"regardless of gate start time; got: {fails}")


def test_stale_cycle_from_previous_release_fails(monkeypatch):
    gate = _gate()
    monkeypatch.setattr(gate, "_get",
                        lambda *a, **k: _status_doc(sha=STALE_SHA))
    fails = gate.check_swarm_cycle("https://prod.example",
                                   expected_sha=DEPLOYED_SHA,
                                   timeout_s=0.5, interval_s=0.0)
    assert fails, "a cycle from a previous release must never certify " \
                  "the new one"


def test_disabled_runner_errored_cycle_and_contact_enabled_all_fail(
        monkeypatch):
    gate = _gate()
    cases = [
        _status_doc(enabled=False),
        _status_doc(error="RuntimeError: adapter meltdown"),
        _status_doc(contact=True),
        _status_doc(completed=False),
    ]
    for doc in cases:
        monkeypatch.setattr(gate, "_get", lambda *a, _d=doc, **k: _d)
        fails = gate.check_swarm_cycle("https://prod.example",
                                       expected_sha=DEPLOYED_SHA,
                                       timeout_s=0.5, interval_s=0.0)
        assert fails, f"gate must fail for status: {doc}"


def test_zero_demand_matching_release_cycle_is_a_legitimate_pass(
        monkeypatch):
    gate = _gate()
    monkeypatch.setattr(
        gate, "_get",
        lambda *a, **k: _status_doc(sha=DEPLOYED_SHA, zero_demand=True))
    fails = gate.check_swarm_cycle("https://prod.example",
                                   expected_sha=DEPLOYED_SHA,
                                   timeout_s=1.0, interval_s=0.0)
    assert fails == []


def test_end_to_end_race_with_the_real_runner(monkeypatch):
    """Full-stack reproduction: the REAL runner completes a cycle (stamped
    with the deployed SHA), THEN the gate starts. The gate must accept it."""
    gate = _gate()
    out = runner.run_once(store, fetch=_no_net)
    assert out["completed"] is True
    time.sleep(0.01)                      # the gate starts strictly later
    monkeypatch.setattr(gate, "_get",
                        lambda *a, **k: runner.status(store))
    fails = gate.check_swarm_cycle("https://prod.example",
                                   expected_sha=DEPLOYED_SHA,
                                   timeout_s=1.0, interval_s=0.0)
    assert fails == [], fails
