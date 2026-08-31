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
    # UTILITY — production only. The mixed/seeded bootstrap lift is NOT printed
    # as a result; a lift with n_recommended == 0 prints "not measurable"
    # rather than a number we would otherwise be quoting from seeded data.
    ("production_measured_lift", "utility · production lift",
     lambda v: f"{v:+.3f}" if isinstance(v, (int, float)) else "not measurable"),
    ("production_n_recommended", "utility · production n_recommended", str),
    # GROWTH — adoption-grade external activity, not "records lacking
    # first_party" (which counts our own untagged tooling and crawlers).
    ("adoption_grade_external_self_claims",
     "growth · external agents holding their OWN credential", str),
    ("external_querying_agents", "growth · external actors querying", str),
    ("external_repeat_query_agents", "retention · repeat-query", str),
    ("external_repeat_paid_agents", "retention · repeat-paid", str),
    # ECONOMIC VALUE — independently confirmed mainnet settlement is the
    # ONLY revenue basis. HEADLINE (2026-08-31): every confirmed mainnet
    # settlement is revenue unless the payer is positively identified as
    # Guild-controlled; attribution is measured, never a prerequisite.
    # Sandbox credits print as credits, never as money.
    ("gross_settled_revenue_usd", "revenue · GROSS settled (USD)",
     lambda v: f"${float(v):.2f}"),
    ("known_first_party_settled_usd",
     "revenue · known first-party/canary settled (USD)",
     lambda v: f"${float(v):.2f}"),
    ("external_settled_revenue_usd",
     "revenue · EXTERNAL settled (USD, not positively first-party)",
     lambda v: f"${float(v):.2f}"),
    ("successful_external_payments", "revenue · external payments", str),
    ("distinct_external_payer_wallets",
     "revenue · distinct external payer wallets", str),
    ("attributed_external_payments",
     "revenue · attributed external payments", str),
    ("attribution_coverage", "revenue · attribution coverage",
     lambda v: (f"{float(v):.0%}" if isinstance(v, (int, float))
                else "n/a (no external payments)")),
    ("verified_external_revenue_usd",
     "revenue · independently ATTESTED subset (USD)",
     lambda v: f"${float(v):.2f}"),
    ("cryptographically_bound_machine_revenue_usd",
     "revenue · bound-machine, ownership unproven (USD)",
     lambda v: f"${float(v):.2f}"),
    ("sandbox_credits_spent_external_NOT_MONEY",
     "sandbox · credits spent (NOT money)", lambda v: f"{v} credits"),
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
    """Same gate as the server verdict: a positive read requires BOTH an
    adoption-grade external credential holder AND external settled mainnet
    revenue (confirmed, payer not positively first-party — founder decision
    2026-08-31). Sandbox credits never qualify."""
    revenue = float(v.get("external_settled_revenue_usd") or 0.0)
    if v["external_querying_agents"] == 0:
        return "NO EXTERNAL DISCOVERY YET — no agent we don't operate has queried the trust layer."
    if v["external_repeat_query_agents"] == 0:
        return "REACH BUT NO RETENTION — queried once, none came back."
    if revenue <= 0:
        return ("NO EXTERNAL SETTLED REVENUE — sandbox credits and first-party "
                "canaries are not money; willingness-to-pay is unproven.")
    return f"EXTERNAL SETTLED REVENUE ${revenue:.2f} — watch the trend."


def fallback_snapshot(base: str) -> dict:
    """Compute from public endpoints when /self-eval is unavailable. Field names
    mirror the server snapshot so the time-series stays uniform."""
    instr = _get(f"{base}/instrumentation") or {}
    ext = instr.get("external", {}) if isinstance(instr, dict) else {}
    ev = _get(f"{base}/evaluation") or {}
    agents = _get(f"{base}/agents") or []
    refs = _get(f"{base}/referrals") or {}
    rev = _get(f"{base}/billing/revenue") or {}
    paid = ext.get("paid_query", 0)
    v = {
        "at": datetime.now(timezone.utc).isoformat(),
        "source": "fallback",
        # production block only — never the mixed/bootstrap top-level lift
        "production_measured_lift": (ev.get("production") or {}).get("lift"),
        "production_n_recommended": int(
            ((ev.get("production") or {}).get("n_recommended")) or 0),
        "mixed_bootstrap_lift_NOT_PRODUCTION": ev.get("lift"),
        "recommended_success_rate": ev.get("recommended_success_rate"),
        "agents_total": len(agents) if isinstance(agents, list) else 0,
        "agents_external": ext.get("unique_agents", 0),
        "external_querying_agents": ext.get("unique_agents", 0),
        "external_repeat_query_agents": ext.get("repeat_query", 0),
        "external_repeat_paid_agents": ext.get("repeat_paid_query_agents", 0),
        "external_paid_queries": paid,
        "sandbox_credits_spent_external_NOT_MONEY": None,
        # Revenue can ONLY come from independently confirmed external mainnet
        # settlement. The old line multiplied sandbox paid-read counts by a
        # notional rate and printed it as USD — that was inventing money.
        # HEADLINE revenue (2026-08-31): straight from the settlement
        # ledger via /billing/revenue.real_settlement.
        "gross_settled_revenue_usd": float(
            ((rev.get("real_settlement") or {}).get(
                "gross_settled_revenue_usd")) or 0.0),
        "known_first_party_settled_usd": float(
            ((rev.get("real_settlement") or {}).get(
                "known_first_party_settled_usd")) or 0.0),
        "external_settled_revenue_usd": float(
            ((rev.get("real_settlement") or {}).get(
                "external_settled_revenue_usd")) or 0.0),
        "successful_external_payments": int(
            ((rev.get("real_settlement") or {}).get(
                "successful_external_payments")) or 0),
        "distinct_external_payer_wallets": int(
            ((rev.get("real_settlement") or {}).get(
                "distinct_external_payer_wallets")) or 0),
        "attributed_external_payments": int(
            ((rev.get("real_settlement") or {}).get(
                "attributed_external_payments")) or 0),
        "attribution_coverage": (rev.get("real_settlement") or {}).get(
            "attribution_coverage"),
        "verified_external_revenue_usd": float(
            ((rev.get("real_settlement") or {}).get(
                "independently_attested_external_revenue_usd")) or 0.0),
        "cryptographically_bound_machine_revenue_usd": float(
            ((rev.get("real_settlement") or {}).get(
                "cryptographically_bound_machine_revenue_usd")) or 0.0),
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
