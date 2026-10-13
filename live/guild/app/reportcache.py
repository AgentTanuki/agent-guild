"""Cached heavy reports (2026-10-13).

Several aggregate reports (/commercial, /funnel/paid, /funnel/paid/actors,
/demand/feed, /funnel, /funnel/passports, /capabilities) replay the whole
durable event history on every request. With the event volume the service
now carries they take 20–90 seconds each, which (a) blocks the single worker
for every other caller, (b) makes any crawler hitting them a denial of
service, and (c) turned the deployment-aware release gate red on 2026-10-13
(live_contract_probe 30 s timeout on /commercial), reverting an unrelated
release. None of these reports needs to be recomputed per request: they
are retired-mandate scorecards and funnels, read by humans and crawlers.

Policy: compute at most once per GUILD_REPORT_CACHE_TTL_S (default 600 s,
0 disables — tests), serve the cached copy with `_report_cache.computed_at`
and `age_s` so a reader can see exactly how fresh it is, protect against
stampedes (concurrent callers wait for the one computation in flight), and
warm the heavy ones in a background thread at startup so a fresh deploy
does not serve its first minute cold.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

_lock = threading.Lock()
_entries: dict[str, dict[str, Any]] = {}
_inflight: dict[str, threading.Lock] = {}


def ttl_s() -> float:
    try:
        return max(0.0, float(os.environ.get("GUILD_REPORT_CACHE_TTL_S", "600")))
    except ValueError:
        return 600.0


def cached(name: str, compute: Callable[[], Any], *,
           ttl: float | None = None) -> Any:
    """Return the cached report for `name`, computing it if missing or older
    than the TTL. Dict results are annotated with `_report_cache`."""
    t = ttl_s() if ttl is None else ttl
    if t <= 0:
        return compute()
    now = time.time()
    with _lock:
        e = _entries.get(name)
        if e and now - e["computed_at"] < t:
            return _annotate(e, now)
        gate = _inflight.setdefault(name, threading.Lock())
    with gate:
        now = time.time()
        with _lock:
            e = _entries.get(name)
            if e and now - e["computed_at"] < t:
                return _annotate(e, now)
        started = time.time()
        value = compute()
        entry = {"value": value, "computed_at": time.time(),
                 "compute_s": round(time.time() - started, 3)}
        with _lock:
            _entries[name] = entry
        return _annotate(entry, time.time())


def _annotate(entry: dict[str, Any], now: float) -> Any:
    v = entry["value"]
    if isinstance(v, dict):
        out = dict(v)
        out["_report_cache"] = {
            "computed_at": _iso(entry["computed_at"]),
            "age_s": int(now - entry["computed_at"]),
            "ttl_s": int(ttl_s()),
            "compute_s": entry["compute_s"],
            "note": "aggregate report served from a cache; recomputed at "
                    "most once per ttl_s",
        }
        return out
    return v


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def clear() -> None:
    with _lock:
        _entries.clear()


def warm(jobs: dict[str, Callable[[], Any]], *, delay_s: float = 2.0) -> None:
    """Compute the given reports once, in a daemon thread, after a short
    delay — so a fresh process serves them warm. Never raises."""
    if ttl_s() <= 0:
        return

    def _run() -> None:
        time.sleep(delay_s)
        for name, fn in jobs.items():
            try:
                cached(name, fn)
            except Exception:  # noqa: BLE001 — warming is best-effort
                pass

    threading.Thread(target=_run, name="report-warm", daemon=True).start()
