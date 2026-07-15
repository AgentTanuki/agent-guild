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
# Captured once at import: the deployment/process start identity every
# persisted cycle is stamped with.
_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
# one kick wakes the loop early — set by stop() AND by a demand wake, so the
# loop can wait on a single event and stay responsive to both.
_kick = threading.Event()


def enabled() -> bool:
    """The explicit production enable switch (Render env)."""
    return (os.environ.get("GUILD_SCOUT_AUTORUN") or "0").strip() == "1"


def release_sha() -> str:
    """The EXACT release identity this process serves — same derivation as
    GET /release. `unknown` is honest and the gate never accepts it."""
    return (os.environ.get("RENDER_GIT_COMMIT")
            or os.environ.get("GUILD_GIT_SHA") or "unknown")


def wake_debounce_s() -> float:
    try:
        return max(0.0, float(
            os.environ.get("GUILD_SCOUT_WAKE_DEBOUNCE_S") or 600.0))
    except ValueError:
        return 600.0


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
    st.setdefault("pending_wake", None)
    st.setdefault("last_wake_at", 0.0)
    st.setdefault("wakes_requested", 0)
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


def notify_demand(store: Any, capability: str) -> bool:
    """Debounced, rate-limited wake signal: newly counted GENUINE external
    unmet demand schedules a prompt scout cycle instead of waiting for the
    interval fallback. Coalesces (one pending wake at a time), respects
    GUILD_SCOUT_WAKE_DEBOUNCE_S between demand-triggered runs, and never
    bypasses the overlap lease (run_once still takes it). Returns True when
    a wake was queued."""
    if not enabled():
        return False
    now = time.time()
    with store.lock:
        st = _state(store)
        if st.get("pending_wake"):
            return False                       # already queued — coalesce
        if now - float(st.get("last_wake_at") or 0.0) < wake_debounce_s():
            return False                       # rate-limited
        st["pending_wake"] = {"reason": "genuine_unmet_demand",
                              "capability": capability, "at": _now_iso()}
        st["last_wake_at"] = now
        st["wakes_requested"] = int(st.get("wakes_requested") or 0) + 1
    _persist(store)
    _kick.set()
    return True


def run_once(store: Any, fetch: Callable = scout.safe_fetch_json,
             owner: str = "", trigger: str = "interval") -> dict[str, Any]:
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
            consumed_wake = st.get("pending_wake")
            st["pending_wake"] = None          # the wake is consumed by this run
            st["last_run"] = {
                "zero_demand": zero_demand,
                "classification": ("zero_demand" if zero_demand
                                   else "demand_cycle"),
                "trigger": trigger,
                "wake": consumed_wake if trigger == "demand_wake" else None,
                # the EXACT release + deployment identity this cycle ran
                # under — the release gate certifies by THIS, not by timing.
                "release_sha": release_sha(),
                "process_started_at": _PROCESS_STARTED_AT,
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
                           trigger=trigger,
                           release_sha=release_sha(),
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
    trigger = "interval"
    while not _stop.is_set():
        try:
            run_once(store, trigger=trigger)
        except Exception as e:  # noqa: BLE001 — the loop must survive anything
            _log.warning("scout runner loop error: %s", e)
        failures = int(_state(store).get("consecutive_failures") or 0)
        # wait for the jittered interval (fallback) OR an early kick —
        # a demand wake or stop(). One pending wake = at most one early run;
        # notify_demand's debounce keeps repeated demand from flooding.
        _kick.clear()
        woke = _kick.wait(next_delay_s(failures=failures))
        if _stop.is_set():
            return
        trigger = ("demand_wake"
                   if woke and _state(store).get("pending_wake")
                   else "interval")


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
    _kick.set()


def status(store: Any) -> dict[str, Any]:
    """The public /swarm/status document — state, never secrets."""
    st = _state(store)
    lease = st.get("lease")
    now = time.time()
    return {
        "enabled": enabled(),
        "contact_enabled": scout.contact_enabled(),
        "running": bool(lease and float(lease.get("expires", 0)) > now),
        # the exact release identity THIS process serves (same derivation
        # as GET /release) — the gate matches cycles against it.
        "release_sha": release_sha(),
        "process_started_at": _PROCESS_STARTED_AT,
        "interval_s": interval_s(),
        "run_timeout_s": run_timeout_s(),
        "last_started_at": st.get("last_started_at"),
        "last_completed_at": st.get("last_completed_at"),
        "last_error": st.get("last_error"),
        "runs_completed": st.get("runs_completed", 0),
        "consecutive_failures": st.get("consecutive_failures", 0),
        # wake visibility: the queued (not yet consumed) wake, if any —
        # reason + capability + timestamp only, never actor identifiers.
        "wake": st.get("pending_wake"),
        "wakes_requested": st.get("wakes_requested", 0),
        "last_run": st.get("last_run"),
        "candidates_recorded": len(
            (store.swarm_state.get(scout.SCOUT_STATE_KEY) or {})
            .get("candidates", {})),
        "note": ("discovery only: candidates are discovered_unverified and "
                 "never contacted (GUILD_SCOUT_CONTACT stays 0), invoked, "
                 "hired or awarded evidence"),
    }
