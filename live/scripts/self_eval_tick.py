#!/usr/bin/env python3
"""Agent Guild — self-evaluation tick (Outcome 4: continuous self-assessment).

One run of the autonomous self-evaluation loop. It reads a *live* Guild's public
read endpoints, computes the health vector across the five objectives — utility,
growth, retention, revenue, referrals — diffs it against the previous snapshot to
show the TREND (not just the level), appends the snapshot to an append-only local
time-series, and prints a blunt, honest verdict.

It depends only on public, free endpoints (`/instrumentation`, `/evaluation`,
`/health`, and `/referrals` / `/self-eval` when present), so it works against the
currently-deployed service and gets richer automatically once newer endpoints
ship. Missing endpoints degrade gracefully rather than failing the tick. Zero
third-party dependencies (stdlib only).

    python self_eval_tick.py --url https://agent-guild-5d5r.onrender.com

Run it on a schedule (e.g. daily) and the Guild assesses itself with no human in
the loop. The verdict is deliberately honest — it says "no external agents yet"
until a genuinely independent agent shows up, so we can't fool ourselves with
self-traffic.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

CREDIT_USD = 0.001


def _get(url: str, timeout: float = 25.0):
    """GET JSON; return None on any error (missing endpoint, timeout, cold start)."""
    try:
        req = urllib.request.Request(url, headers={"accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
        return None


def _verdict(v: dict) -> str:
    if v["agents_external"] == 0 and v["external_querying_agents"] == 0:
        return ("NO EXTERNAL ACTIVITY YET — until an agent we don't operate calls, "
                "every number is self-traffic.")
    if v["external_querying_agents"] == 0:
        return ("REGISTRATIONS BUT NO DISCOVERY — external agents exist but none have "
                "queried the trust layer; the core product is untested in the wild.")
    if v["external_repeat_query_agents"] == 0:
        return ("REACH BUT NO RETENTION — outside agents queried once but none came "
                "back; usefulness unproven.")
    if v["external_paid_queries"] == 0:
        return ("RETENTION BUT NO WILLINGNESS-TO-PAY — agents return for free reads but "
                "none spend their own budget yet.")
    return ("WILLINGNESS-TO-PAY CONFIRMED — external agents return AND pay. Watch the "
            "trend: is paid usage and the agent base growing period over period?")


def compute_snapshot(base: str) -> dict:
    instr = _get(f"{base}/instrumentation") or {}
    ext = instr.get("external", {}) if isinstance(instr, dict) else {}
    ev = _get(f"{base}/evaluation") or {}
    agents = _get(f"{base}/agents") or []
    refs = _get(f"{base}/referrals")  # 404 until the referral primitive ships
    # paid reads × headline price is a conservative revenue proxy until the live
    # billing ledger is exposed read-only.
    paid = ext.get("paid_query", 0)
    v = {
        "at": datetime.now(timezone.utc).isoformat(),
        "live_url": base,
        # utility
        "measured_lift": ev.get("lift"),
        "recommended_success_rate": ev.get("recommended_success_rate"),
        # growth
        "agents_total": len(agents) if isinstance(agents, list) else 0,
        "agents_external": (ext.get("unique_agents", 0)),  # proxy: distinct external actors
        # retention
        "external_querying_agents": ext.get("unique_agents", 0),
        "external_repeat_query_agents": ext.get("repeat_query", 0),
        "external_repeat_paid_agents": ext.get("repeat_paid_query_agents", 0),
        # revenue
        "external_paid_queries": paid,
        "revenue_usd_proxy": round(paid * 10 * CREDIT_USD, 4),  # ~best-agent price
        # referrals (0 until the endpoint exists)
        "total_referrals": (refs or {}).get("total_referrals", 0),
        "activated_referrals": (refs or {}).get("activated_referrals", 0),
        "referrals_endpoint_live": refs is not None,
    }
    v["verdict"] = _verdict(v)
    return v


def main() -> int:
    ap = argparse.ArgumentParser(description="Record one Agent Guild health snapshot.")
    ap.add_argument("--url", default=os.environ.get("GUILD_URL",
                    "https://agent-guild-5d5r.onrender.com"),
                    help="Base URL of the live Guild.")
    default_out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "experiments", "results", "health_history.jsonl")
    ap.add_argument("--out", default=os.path.normpath(default_out),
                    help="Append-only JSONL time-series of health snapshots.")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    if _get(f"{base}/health") is None:
        print(f"[self-eval] {base} unreachable (cold start or down); skipping tick.",
              file=sys.stderr)
        return 1

    snap = compute_snapshot(base)

    # Trend vs the previous recorded snapshot.
    prev = None
    if args.out and os.path.exists(args.out):
        try:
            with open(args.out) as f:
                lines = [ln for ln in f if ln.strip()]
            if lines:
                prev = json.loads(lines[-1])
        except (OSError, ValueError):
            prev = None
    deltas = {}
    if prev:
        for k, val in snap.items():
            if isinstance(val, (int, float)) and isinstance(prev.get(k), (int, float)):
                deltas[k] = round(val - prev[k], 4)
    snap["deltas"] = deltas

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "a") as f:
            f.write(json.dumps(snap) + "\n")

    def arrow(k):
        d = deltas.get(k, 0)
        return f" ({'+' if d > 0 else ''}{d})" if d else ""

    lift = snap["measured_lift"]
    lift_s = f"{lift:+.3f}" if isinstance(lift, (int, float)) else "n/a"
    print(f"=== Agent Guild self-evaluation @ {snap['at']} ===")
    print(f"  VERDICT: {snap['verdict']}")
    print(f"  utility   : measured_lift {lift_s} · recommended_success "
          f"{snap['recommended_success_rate']}")
    print(f"  growth    : distinct external actors {snap['agents_external']}{arrow('agents_external')}")
    print(f"  retention : repeat-query {snap['external_repeat_query_agents']}"
          f"{arrow('external_repeat_query_agents')} · repeat-paid "
          f"{snap['external_repeat_paid_agents']}{arrow('external_repeat_paid_agents')}")
    print(f"  revenue   : paid reads {snap['external_paid_queries']}"
          f"{arrow('external_paid_queries')} · ~${snap['revenue_usd_proxy']}{arrow('revenue_usd_proxy')}")
    print(f"  referrals : {snap['total_referrals']}{arrow('total_referrals')} "
          f"(activated {snap['activated_referrals']}; endpoint_live={snap['referrals_endpoint_live']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
