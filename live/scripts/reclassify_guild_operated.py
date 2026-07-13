#!/usr/bin/env python3
"""Reclassify Guild-OPERATED agents as first-party on the live service
(corrective pass 2026-07-13) — append-only, never rewrites history.

`live/market_clients/external_buyer.py` (and earlier ad-hoc runs) registered
buyer identities WITHOUT first-party authentication, so their telemetry
counted as external. This script finds those Guild-operated identities and
flags each via POST /admin/agents/{id}/first-party — the admin endpoint sets
the first_party flag on the agent + its accounts and appends an audit event;
immutable ledger/transaction history is untouched (attribution composes at
read time, exactly like provenance reclassification).

Usage:
    GUILD_ADMIN_TOKEN=... python3 live/scripts/reclassify_guild_operated.py \
        [--base https://agent-guild-5d5r.onrender.com] [--apply]

Without --apply it only lists what it WOULD flag.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

# name prefixes of identities Agent Guild itself operates
GUILD_OPERATED_PREFIXES = (
    "ExternalBuyer-Py-",     # live/market_clients/external_buyer.py
    "GuildBuyer-",           # live/market_clients/buyer/buyer.mjs
    "trustplane-gateway-",   # gateway outcome reporters run by us
)


def _get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get(
        "GUILD_URL", "https://agent-guild-5d5r.onrender.com"))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    token = os.environ.get("GUILD_ADMIN_TOKEN", "")
    agents = _get(base, "/agents")
    rows = agents.get("agents", agents) if isinstance(agents, dict) else agents
    targets = [a for a in rows
               if any(str(a.get("name", "")).startswith(p)
                      for p in GUILD_OPERATED_PREFIXES)
               and not a.get("first_party")]
    print(f"{len(targets)} Guild-operated agent(s) still classified external:")
    for a in targets:
        print(f"  {a['id']}  {a['name']}")
    if not args.apply:
        print("dry run — pass --apply with GUILD_ADMIN_TOKEN to flag them")
        return 0
    if not token:
        print("GUILD_ADMIN_TOKEN required for --apply", file=sys.stderr)
        return 2
    for a in targets:
        req = urllib.request.Request(
            f"{base}/admin/agents/{a['id']}/first-party", method="POST",
            headers={"X-Admin-Token": token})
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"flagged {a['id']}: {r.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
