"""The production scout runner — the missing piece of scout autonomy.

`run_scout()` existed but nothing in production invoked it: "autonomy"
required a human or an external button. This module runs it as a background
loop inside the service process:

  * EXPLICITLY enabled: GUILD_SCOUT_AUTORUN=1 (set in Render). Default OFF —
    a test suite, a local checkout or an unconfigured deploy never scouts.
  * a persisted LEASE (swarm_state["scout_runner"]["lease"]) prevents
    overlapping runs — across threads AND across restarts/second processes.
    A stale lease (holder crashed) is recoverable after its TTL.
  * every cycle is bounded: a per-run deadline (GUILD_SCOUT_RUN_TIMEOUT_S,
    default 120s) rides into run_scout(); fetches/probes are already
    individually bounded and SSRF-safe.
  * the schedule is jittered (±20% of GUILD_SCOUT_INTERVAL_S, default 6h)
    and backs off exponentially on consecutive failures.
  * state (last start/completion, per-adapter results, discovered/refreshed
    counts, last error, zero-demand flag) is persisted and served by
    GET /swarm/status — no secrets.
  * outbound contact stays OFF: the runner never calls maybe_contact; the
    GUILD_SCOUT_CONTACT gate is untouched and independently default-OFF.
  * a ZERO-DEMAND cycle (no genuine unmet demand to scout for) is a
    legitimate COMPLETED cycle — the release gate accepts it.
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from . import scout

_log = logging.getLogger("agent-guild.scout-runner")

RUNNER_STATE_KEY = "scout_runner"
_PROCESS_OWNER = "runner-" + uuid.uuid4().hex[:12]
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def enabled() -> bool:
    """The explicit production enable switch (Render env)."""
    return (os.environ.get("GUILD_SCOUT_AUTORUN") or "0").strip() == "1"


def interval_s() -> float:
    try:
        return max(60.0, float(
            os.environ.get("GUILD_SCOUT_INTERVAL_S") or 21600.0))
    except ValueError:
        return 21600.0


def run_timeout_s() -> float:
    try:
        return max(10.0, float(
            os.environ.get("GUILD_SCOUT_RUN_TIMEOUT_S") or 120.0))
    except ValueError:
        return 120.0


def initial_delay_s() -> float:
    """Small jittered delay after boot so the first post-deploy cycle
    completes quickly (the release gate waits for it) without a thundering
    start during process churn."""
    try:
        base = float(os.environ.get("GUILD_SCOUT_INITIAL_DELAY_S") or 15.0)
    except ValueError:
        base = 15.0
    return base + random.uniform(0, base)


def next_delay_s(failures: int = 0) -> float:
    """Jittered schedule with exponential backoff on consecutive failures,
    never exceeding the configured interval."""
    if failures > 0:
        backoff = min(interval_s(), 60.0 * (2 ** min(failures, 10)))
        return backoff * random.uniform(0.8, 1.2)
    return interval_s() * random.uniform(0.8, 1.2)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state(store: Any) -> dict[str, Any]:
    st = store.swarm_state.setdefault(RUNNER_STATE_KEY, {})
    st.setdefault("lease", None)
    st.setdefault("last_started_at", None)
    st.setdefault("last_completed_at", None)
    st.setdefault("last_error", None)
    st.setdefault("last_run", None)
    st.setdefault("runs_completed", 0)
    st.setdefault("consecutive_failures", 0)
    return st


def _persist(store: Any) -> None:
    with store.lock, store._txn():
        if store.backend is not None:
            store._persist_kv("swarm_state", store.swarm_state)
        store._save()


def _acquire_lease(store: Any, owner: str, ttl_s: float,
                   now: Optional[float] = None) -> bool:
    """Take the persisted run lease iff it is free, expired, or already ours.
    Persisted BEFORE the run starts, so a second process (or a restart mid-
    run) can never overlap; a crashed holder's lease recovers after TTL."""
    now = time.time() if now is None else now
    with store.lock:
        st = _state(store)
        lease = st.get("lease")
        if (lease and lease.get("owner") != owner
                and float(lease.get("expires", 0)) > now):
            return False
        st["lease"] = {"owner": owner, "expires": now + ttl_s,
                       "taken_at": now}
    _persist(store)
    return True


def _release_lease(store: Any, owner: str) -> None:
    with store.lock:
        st = _state(store)
        lease = st.get("lease")
        if lease and lease.get("owner") == owner:
            st["lease"] = None
    _persist(store)


def _run_scout(store: Any, *, fetch: Callable, deadline: float,
               ) -> dict[str, Any]:
    """Isolated for tests."""
    return scout.run_scout(store, fetch=fetch, deadline=deadline)


def run_once(store: Any, fetch: Callable = scout.safe_fetch_json,
             owner: str = "") -> dict[str, Any]:
    """One guarded production cycle: enable-check → lease → bounded
    run_scout → persisted status. Returns {"completed": bool, ...}."""
    owner = owner or _PROCESS_OWNER
    if not enabled():
        return {"completed": False, "reason": "disabled"}
    ttl = run_timeout_s() + 30.0
    if not _acquire_lease(store, owner, ttl):
        return {"completed": False, "reason": "lease_held_by_other_runner"}
    st = _state(store)
    st["last_started_at"] = _now_iso()
    _persist(store)
    try:
        deadline = time.time() + run_timeout_s()
        summary = _run_scout(store, fetch=fetch, deadline=deadline)
        zero_demand = not summary.get("capabilities")
        with store.lock:
            st = _state(store)
            st["last_completed_at"] = _now_iso()
            st["last_error"] = None
            st["runs_completed"] = int(st.get("runs_completed") or 0) + 1
            st["consecutive_failures"] = 0
            st["last_run"] = {
                "zero_demand": zero_demand,
                "capabilities": summary.get("capabilities", []),
                "discovered": summary.get("discovered", 0),
                "refreshed": summary.get("refreshed", 0),
                "endpoint_verified": summary.get("endpoint_verified", 0),
                "adapters": summary.get("adapters", {}),
                "adapters_failed": summary.get("adapters_failed", []),
                "deadline_hit": bool(summary.get("deadline_hit")),
            }
        _persist(store)
        store.record_event(None, "scout_cycle_completed",
                           zero_demand=zero_demand,
                           discovered=summary.get("discovered", 0),
                           refreshed=summary.get("refreshed", 0),
                           scout=True)
        return {"completed": True, "zero_demand": zero_demand,
                "summary": st["last_run"]}
    except Exception as e:  # noqa: BLE001
        with store.lock:
            st = _state(store)
            st["last_error"] = f"{type(e).__name__}: {e}"[:300]
            st["consecutive_failures"] = int(
                st.get("consecutive_failures") or 0) + 1
        _persist(store)
        _log.warning("scout cycle failed: %s", e)
        return {"completed": False, "reason": "run_failed",
                "error": st["last_error"]}
    finally:
        _release_lease(store, owner)


def _loop(store: Any) -> None:
    if _stop.wait(initial_delay_s()):
        return
    while not _stop.is_set():
        try:
            run_once(store)
        except Exception as e:  # noqa: BLE001 — the loop must survive anything
            _log.warning("scout runner loop error: %s", e)
        failures = int(_state(store).get("consecutive_failures") or 0)
        if _stop.wait(next_delay_s(failures=failures)):
            return


def start(store: Any) -> bool:
    """Start the background runner thread (idempotent). Called from the app
    lifespan; a no-op unless GUILD_SCOUT_AUTORUN=1."""
    global _thread
    if not enabled():
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(store,),
                               name="guild-scout-runner", daemon=True)
    _thread.start()
    _log.info("scout runner started (interval ~%ss, run timeout %ss)",
              int(interval_s()), int(run_timeout_s()))
    return True


def stop() -> None:
    _stop.set()


def status(store: Any) -> dict[str, Any]:
    """The public /swarm/status document — state, never secrets."""
    st = _state(store)
    lease = st.get("lease")
    now = time.time()
    return {
        "enabled": enabled(),
        "contact_enabled": scout.contact_enabled(),
        "running": bool(lease and float(lease.get("expires", 0)) > now),
        "interval_s": interval_s(),
        "run_timeout_s": run_timeout_s(),
        "last_started_at": st.get("last_started_at"),
        "last_completed_at": st.get("last_completed_at"),
        "last_error": st.get("last_error"),
        "runs_completed": st.get("runs_completed", 0),
        "consecutive_failures": st.get("consecutive_failures", 0),
        "last_run": st.get("last_run"),
        "candidates_recorded": len(
            (store.swarm_state.get(scout.SCOUT_STATE_KEY) or {})
            .get("candidates", {})),
        "note": ("discovery only: candidates are discovered_unverified and "
                 "never contacted (GUILD_SCOUT_CONTACT stays 0), invoked, "
                 "hired or awarded evidence"),
    }
