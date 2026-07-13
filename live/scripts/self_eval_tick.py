#!/usr/bin/env python3
"""Agent Guild — self-evaluation tick (Outcome 4: continuous self-assessment).

One run of the autonomous self-evaluation loop. It pulls the Guild's own health
snapshot from the read-only `/self-eval` endpoint — the SINGLE SOURCE OF TRUTH,
computed server-side across the five objectives (utility, growth, retention,
revenue, referrals) with a verdict — appends it to a local append-only
time-series, and prints the verdict plus what moved versus last run.

Because the verdict and every metric come from the server, this monitoring loop
and the server's own self-assessment can never diverge. If `/self-eval` is
unavailable (older deployment or cold start), it degrades to a clearly-labelled
FALLBACK computed from public endpoints, so a tick is never silently lost.

Zero third-party dependencies (stdlib only).

    python self_eval_tick.py --url https://agent-guild-5d5r.onrender.com
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
try:
    from _firstparty_headers import first_party_headers as _fp_headers
except ImportError:  # run from another cwd
    import sys as _sys, pathlib as _pl
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    from _firstparty_headers import first_party_headers as _fp_headers

import urllib.error
from datetime import datetime, timezone

CREDIT_USD = 0.001

# Fields we surface in the printed summary, in order: (key, label, formatter).
DISPLAY = [
    ("verdict", None, None),
    ("measured_lift", "utility · measured_lift", lambda v: f"{v:+.3f}" if isinstance(v, (int, float)) else "n/a"),
    ("agents_external", "growth · external agents", str),
    ("external_querying_agents", "growth · external actors querying", str),
    ("external_repeat_query_agents", "retention · repeat-query", str),
    ("external_repeat_paid_agents", "retention · repeat-paid", str),
    ("external_paid_queries", "revenue · paid reads", str),
    ("revenue_usd_external", "revenue · USD", lambda v: f"${v}"),
    ("total_referrals", "referrals · total", str),
    ("activated_referrals", "referrals · activated", str),
]


def _get(url: str, timeout: float = 25.0):
    try:
        req = urllib.request.Request(url, headers={"accept": "application/json", **_fp_headers()})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
        return None


def _fallback_verdict(v: dict) -> str:
    if v["external_querying_agents"] == 0:
        return "NO EXTERNAL DISCOVERY YET — no agent we don't operate has queried the trust layer."
    if v["external_repeat_query_agents"] == 0:
        return "REACH BUT NO RETENTION — queried once, none came back."
    if v["external_paid_queries"] == 0:
        return "RETENTION BUT NO WILLINGNESS-TO-PAY — agents return for free reads but none pay."
    return "WILLINGNESS-TO-PAY PRESENT — external agents return and pay; watch the trend."


def fallback_snapshot(base: str) -> dict:
    """Compute from public endpoints when /self-eval is unavailable. Field names
    mirror the server snapshot so the time-series stays uniform."""
    instr = _get(f"{base}/instrumentation") or {}
    ext = instr.get("external", {}) if isinstance(instr, dict) else {}
    ev = _get(f"{base}/evaluation") or {}
    agents = _get(f"{base}/agents") or []
    refs = _get(f"{base}/referrals") or {}
    paid = ext.get("paid_query", 0)
    v = {
        "at": datetime.now(timezone.utc).isoformat(),
        "source": "fallback",
        "measured_lift": ev.get("lift"),
        "recommended_success_rate": ev.get("recommended_success_rate"),
        "agents_total": len(agents) if isinstance(agents, list) else 0,
        "agents_external": ext.get("unique_agents", 0),
        "external_querying_agents": ext.get("unique_agents", 0),
        "external_repeat_query_agents": ext.get("repeat_query", 0),
        "external_repeat_paid_agents": ext.get("repeat_paid_query_agents", 0),
        "external_paid_queries": paid,
        "credits_spent_external": None,
        "revenue_usd_external": round(paid * 10 * CREDIT_USD, 4),
        "total_referrals": refs.get("total_referrals", 0),
        "activated_referrals": refs.get("activated_referrals", 0),
    }
    v["verdict"] = "[FALLBACK] " + _fallback_verdict(v)
    return v


def main() -> int:
    ap = argparse.ArgumentParser(description="Record one Agent Guild health snapshot.")
    ap.add_argument("--url", default=os.environ.get("GUILD_URL",
                    "https://agent-guild-5d5r.onrender.com"))
    default_out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "experiments", "results", "health_history.jsonl")
    ap.add_argument("--out", default=os.path.normpath(default_out))
    args = ap.parse_args()
    base = args.url.rstrip("/")

    if _get(f"{base}/health") is None:
        print(f"[self-eval] {base} unreachable (cold start or down); skipping tick.",
              file=sys.stderr)
        return 1

    # Primary: the server's own snapshot (single source of truth).
    snap = _get(f"{base}/self-eval")
    if isinstance(snap, dict) and "verdict" in snap:
        snap["source"] = "server"
    else:
        snap = fallback_snapshot(base)

    # Local trend vs the previously recorded tick (independent of server deltas,
    # so the local series is self-consistent even across deploys).
    prev = None
    if args.out and os.path.exists(args.out):
        try:
            lines = [ln for ln in open(args.out) if ln.strip()]
            if lines:
                prev = json.loads(lines[-1])
        except (OSError, ValueError):
            prev = None
    deltas = {}
    if prev:
        for k, val in snap.items():
            if isinstance(val, (int, float)) and isinstance(prev.get(k), (int, float)):
                deltas[k] = round(val - prev[k], 4)
    snap["local_deltas"] = deltas

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "a") as f:
            f.write(json.dumps(snap) + "\n")

    def arrow(k):
        d = deltas.get(k, 0)
        return f" ({'+' if d > 0 else ''}{d})" if d else ""

    print(f"=== Agent Guild self-evaluation @ {snap.get('at')} (source: {snap.get('source')}) ===")
    for key, label, fmt in DISPLAY:
        if key == "verdict":
            print(f"  VERDICT: {snap.get('verdict', '')}")
            continue
        if key not in snap or snap[key] is None:
            continue
        val = fmt(snap[key]) if fmt else snap[key]
        print(f"  {label}: {val}{arrow(key)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
