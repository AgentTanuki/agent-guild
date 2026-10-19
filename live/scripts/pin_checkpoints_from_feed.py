#!/usr/bin/env python3
"""Pin the PUBLIC checkpoint feed into docs/checkpoints/ — no secrets.

Reads GET /ledger/checkpoints (public), writes every entry that is not yet
pinned as docs/checkpoints/checkpoint-<index>.json plus latest.json, and
prints the indexes it added. Sealing happens on the service itself (swarm
runner, daily); this script only copies what the service already serves,
so an independent party can diff the repo against the live feed at any time.
Exit 0 whether or not anything changed; prints CHANGED=<n> on the last line.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.request

HOST = os.environ.get("GUILD_HOST", "https://agent-guild-5d5r.onrender.com")
REPO = pathlib.Path(__file__).resolve().parents[2]
OUTDIR = REPO / "docs" / "checkpoints"


def main() -> int:
    # The feed caps `limit` at 200 (Query(le=200)); asking for 500 was a 422
    # that failed the pin workflow five days running (10-14 → 10-18) and let
    # the repo pin fall behind the sealed head. 200 is the whole feed today.
    with urllib.request.urlopen(f"{HOST}/ledger/checkpoints?limit=200",
                                timeout=60) as r:
        items = json.loads(r.read().decode("utf-8"))
    if isinstance(items, dict):
        items = items.get("checkpoints") or items.get("items") or []
    OUTDIR.mkdir(parents=True, exist_ok=True)
    added = []
    for e in sorted(items, key=lambda x: int(x["index"])):
        path = OUTDIR / f"checkpoint-{int(e['index']):05d}.json"
        body = json.dumps(e, indent=1, sort_keys=True) + "\n"
        if path.exists() and path.read_text() == body:
            continue
        if path.exists():
            # A pinned entry that differs from the feed is a finding, not a
            # silent overwrite: keep the pin and report.
            print(f"MISMATCH index={e['index']} (pinned copy kept)", file=sys.stderr)
            continue
        path.write_text(body)
        added.append(int(e["index"]))
    if items:
        head = max(items, key=lambda x: int(x["index"]))
        (OUTDIR / "latest.json").write_text(
            json.dumps(head, indent=1, sort_keys=True) + "\n")
    print(f"feed entries={len(items)} added={added}")
    print(f"CHANGED={len(added)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
