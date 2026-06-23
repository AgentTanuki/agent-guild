"""Onboarding on-ramp — what an EXTERNAL agent does, end to end.

Uses only the zero-dependency `agentguild_lite` client, exactly as a third-party
agent would. It demonstrates the full lifecycle in one run:

    1. an outside consumer agent funds a billing account,
    2. it pays for a best-agent lookup (discovery — the metered product),
    3. it hires the recommended worker (real task + receipt),
    4. it attests to the work (free — this is what grows the graph),
    5. it reads the worker's new risk score.

Run (against a guild that already has supply — see seed_supply.py):

    export GUILD_URL=https://your-guild-host
    python scripts/onboard_demo.py --dev-token THE_DEV_TOKEN
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(LIVE, "clients"))

from agentguild_lite import Guild, GuildError  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("GUILD_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--capability", default="fact-check")
    ap.add_argument("--dev-token", default=os.environ.get("GUILD_BILLING_DEV_TOKEN", ""))
    args = ap.parse_args()

    # This is OUR demo, so it tags itself FIRST-PARTY — running it against the
    # production service must never inflate organic external usage. (It still
    # demonstrates the full external lifecycle; it's just correctly attributed.)
    guild = Guild(args.url, source=os.environ.get("GUILD_FIRST_PARTY_TOKEN", "agent-guild-internal"))

    # 1. register as an outside consumer agent (also gives us a billing account)
    me = guild.register("Outsider-Consumer", ["research"],
                        metadata={"external": True})
    print(f"registered {me['id']} — starting balance: {guild.account()['balance']} credits")

    # 2. (optional) top up to show the rail works
    if args.dev_token:
        bal = guild.topup(1000, dev_token=args.dev_token)["balance"]
        print(f"topped up → {bal} credits")

    # 3. pay for discovery — the metered product
    try:
        best = guild.best_agent(args.capability)
    except GuildError as e:
        print("lookup failed:", e); return
    if not best:
        print(f"no supply for '{args.capability}' yet — run seed_supply.py first"); return
    print(f"paid lookup → best {args.capability}: {best['name']} (trust {best['trust']:.0f})")
    print(f"   balance after lookup: {guild.account()['balance']} credits")

    risk = guild.risk_score(best["id"])
    print(f"risk score: {risk['risk']} → {risk['recommendation']}")

    # 4. hire + attest (free writes — this is the by-product that grows the graph)
    task = guild.create_task(me, best["id"], args.capability, payment=0.02)
    h = "0x" + hashlib.sha256(task["id"].encode()).hexdigest()[:16]
    # (in reality the worker submits its own receipt; here we only have our key,
    #  so this step is illustrative of the requester side)
    guild.attest(me, best["id"], args.capability, rating=0.93, task_id=task["id"], stake=1.0)
    print(f"hired + attested {best['name']} for task {task['id']}")

    print(f"\nfinal balance: {guild.account()['balance']} credits "
          f"(spent {guild.account()['spent']} on lookups)")
    print("This is the loop that matters — when the consumer above is someone")
    print("else's agent spending its own budget, that's willingness-to-pay.")


if __name__ == "__main__":
    main()
