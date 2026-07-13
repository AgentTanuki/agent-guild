"""Abuse controls (production-truth hardening, 2026-07-13).

Free writes are the Guild's growth engine, which also makes them the obvious
attack surface. This module bounds the four cheap-to-mount abuses:

  * registration flooding        — identity spam to farm listings
  * trial-credit farming         — repeated /billing/trial from one origin
  * expensive-read bursts        — unfunded scraping of priced reads
  * storage exhaustion           — oversized bodies / deliverables / watches

Mechanism: in-memory sliding-window limits keyed by client IP (the platform
proxy sets the client address; uvicorn runs with --proxy-headers), plus hard
size caps. All limits are env-tunable (GUILD_RL_*) and the whole subsystem can
be disabled with GUILD_ABUSE_CONTROLS=0 (tests do this; production does not).

In-memory state is per-process and resets on restart — that is acceptable for
these limits (they bound burst rates, not lifetime counts) and keeps the hot
path allocation-free. SQLite/Postgres-backed quotas are the scale-up path.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

from fastapi import HTTPException, Request

_lock = threading.Lock()
_hits: dict[tuple[str, str], list[float]] = {}

# bucket -> (max hits, window seconds, env prefix)
_DEFAULTS = {
    "register": (30, 3600, "REGISTER"),        # identities per IP per hour
    "trial": (5, 86400, "TRIAL"),              # trial grants per IP per day
    "read_burst": (240, 60, "READ_BURST"),     # unfunded priced reads per IP/min
    "write_burst": (120, 3600, "WRITE_BURST"), # collaborations/attestations per IP/hr
    "demand_watch": (60, 3600, "DEMAND_WATCH"),
}

MAX_BODY_BYTES = int(os.environ.get("GUILD_MAX_BODY_BYTES", 262144))        # 256 KiB
MAX_DELIVERABLE_BYTES = int(os.environ.get("GUILD_MAX_DELIVERABLE_BYTES", 65536))


def enabled() -> bool:
    return os.environ.get("GUILD_ABUSE_CONTROLS", "1") != "0"


def _config(bucket: str) -> tuple[int, float]:
    mx, window, name = _DEFAULTS[bucket]
    return (int(os.environ.get(f"GUILD_RL_{name}", mx)),
            float(os.environ.get(f"GUILD_RL_{name}_WINDOW_S", window)))


def client_ip(request: Optional[Request]) -> str:
    if request is None or request.client is None:
        return "unknown"
    return request.client.host or "unknown"


def check(bucket: str, key: str, max_hits: int, window_s: float) -> None:
    """Sliding-window limit; raises a machine-readable 429 when exceeded."""
    now = time.time()
    k = (bucket, key)
    with _lock:
        hits = [t for t in _hits.get(k, []) if now - t < window_s]
        if len(hits) >= max_hits:
            retry = int(window_s - (now - hits[0])) + 1
            raise HTTPException(429, {
                "error": "rate_limited",
                "bucket": bucket,
                "limit": max_hits,
                "window_seconds": int(window_s),
                "retry_after_seconds": max(retry, 1),
            })
        hits.append(now)
        _hits[k] = hits
        # bound the limiter's own memory (storage-exhaustion guard for the guard)
        if len(_hits) > 50000:
            cutoff = now - 86400
            for kk in list(_hits):
                if not _hits[kk] or _hits[kk][-1] < cutoff:
                    del _hits[kk]


def guard(request: Optional[Request], bucket: str) -> None:
    """Apply the configured limit for `bucket` to the request's client IP."""
    if not enabled():
        return
    mx, window = _config(bucket)
    check(bucket, client_ip(request), mx, window)


def reset() -> None:
    """Test helper: clear all limiter state."""
    with _lock:
        _hits.clear()
