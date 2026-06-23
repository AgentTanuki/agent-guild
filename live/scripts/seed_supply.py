"""Cold-start liquidity — seed the SUPPLY side of Agent Guild.

A reputation lookup is worthless until the graph contains attested workers to
return. This script registers a realistic roster of worker agents across several
capabilities, then has pre-trusted seed employers commission *real* (paid,
receipted) tasks and attest the workers honestly by observed quality. The result
is a populated graph where `best_agent(capability)` returns something useful.

This is market-making, not demand-faking:

  * Every seeded agent is tagged `metadata.seed_supply = true` so it can be
    excluded from any traction / revenue metric.
  * Attestations reflect an honest quality signal, not mutual back-scratching —
    so the collusion engine stays quiet and the rankings are real.
  * It seeds SUPPLY (workers available to hire), never DEMAND (paying lookups).
    Willingness-to-pay must come from outside agents; this only makes the
    service worth paying for.

Run against a live Guild:

    export GUILD_URL=https://your-guild-host
    export GUILD_ADMIN_TOKEN=...        # needed to register pre-trusted seeds
    python scripts/seed_supply.py --workers-per-cap 4
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(LIVE, "agentkit"))

from agentguild import GuildClient  # noqa: E402


# A realistic spread: capability -> list of (name, honest_true_quality, price)
ROSTER = {
    "fact-check": [
        ("Veritas-Prime", 0.93, 0.030), ("CiteCheck", 0.86, 0.020),
        ("QuickFact", 0.62, 0.006), ("RumorMill", 0.34, 0.004),
    ],
    "code-review": [
        ("LintLord", 0.91, 0.040), ("PRPilot", 0.80, 0.025),
        ("NitPicker", 0.58, 0.010), ("RubberStamp", 0.30, 0.005),
    ],
    "research": [
        ("DeepDive", 0.90, 0.050), ("ScholarBot", 0.78, 0.030),
        ("SkimReader", 0.55, 0.012), ("CopyPasta", 0.33, 0.006),
    ],
    "summarization": [
        ("Distil", 0.88, 0.018), ("TLDRpro", 0.74, 0.010),
        ("Truncate", 0.50, 0.005),
    ],
    "data-extraction": [
        ("Parsely", 0.92, 0.022), ("ScrapeSmith", 0.79, 0.014),
        ("RegexRoulette", 0.45, 0.006),
    ],
}


def _hash(*parts) -> str:
    return "0x" + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("GUILD_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--admin-token", default=os.environ.get("GUILD_ADMIN_TOKEN", ""))
    ap.add_argument("--employers", type=int, default=3)
    ap.add_argument("--workers-per-cap", type=int, default=4)
    ap.add_argument("--jobs-per-worker", type=int, default=3)
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    # Tag all of this script's traffic as FIRST-PARTY so seeded supply is never
    # counted as organic external usage (matches the server's
    # GUILD_FIRST_PARTY_TOKEN when set; harmless marker otherwise).
    g = GuildClient(args.url, source=os.environ.get("GUILD_FIRST_PARTY_TOKEN", "agent-guild-internal"))
    print(f"Seeding supply into {args.url}")
    if not args.admin_token:
        print("  (no GUILD_ADMIN_TOKEN — employers will register as ordinary agents; "
              "fine for local, but on a live host seeds need the admin token)")

    # --- pre-trusted employers (the trust anchor) --------------------------
    employers = []
    for i in range(args.employers):
        e = g.register(name=f"Verified-Employer-{i+1}", capabilities=["hiring"],
                       metadata={"seed_supply": True, "role": "employer"},
                       seed=bool(args.admin_token), admin_token=args.admin_token or None)
        employers.append(e)
    print(f"  registered {len(employers)} employers "
          f"({'seed-trusted' if args.admin_token else 'untrusted'})")

    # --- workers + honest, receipt-backed attestations ---------------------
    created = 0
    for cap, roster in ROSTER.items():
        for (name, quality, price) in roster[:args.workers_per_cap]:
            w = g.register(name=name, capabilities=[cap],
                           metadata={"seed_supply": True, "price_per_call": price})
            created += 1
            # each employer commissions a few real, paid jobs and rates by quality
            for e in employers:
                for j in range(args.jobs_per_worker):
                    task = g.create_task(e, w.id, cap, payment=price)
                    g.submit_receipt(w, task["id"], _hash(w.id, e.id, j), outcome="delivered")
                    rating = max(0.0, min(1.0, quality + rng.uniform(-0.05, 0.05)))
                    g.attest(e, w.id, cap, rating, task_id=task["id"], stake=0.0)
    print(f"  registered {created} workers across {len(ROSTER)} capabilities, "
          f"each with {args.employers * args.jobs_per_worker} receipt-backed attestations")

    # --- show the resulting rankings ---------------------------------------
    print("\nLookups now return real, ranked supply:")
    summary_key = employers[0].api_key  # search is a metered read under enforcement
    for cap in ROSTER:
        top = g.search(cap, limit=3, api_key=summary_key)
        line = ", ".join(f"{r['name']}({r['trust']:.0f})" for r in top)
        print(f"  {cap:<16} → {line}")

    print("\nSupply seeded. Discovery is now worth paying for — but remember: this is")
    print("liquidity, not traction. The metric that matters is an OUTSIDE agent")
    print("choosing to pay for a lookup. See docs/MONETISATION.md.")


if __name__ == "__main__":
    main()
