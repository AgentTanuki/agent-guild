#!/usr/bin/env python3
"""Agent Guild — outreach loop (distribution pipeline + tracker).

This is NOT autonomous recruitment: nothing here contacts another agent. It is a
tracked pipeline of REAL distribution actions (registry submissions, awesome-list
PRs, directory listings, framework/package distribution) plus the success metric
that matters — whether any of it produced a genuine external agent.

Commands:
  status   (default) summarise the pipeline + report the first-external-agent signal
  next     print pending, actionable targets with the ready-to-send copy
  check    live-verify our footprint (is the official registry listing active?)
  log ID STATUS [EVIDENCE]   append an executed outreach action to the log

Zero third-party dependencies.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # live/
OUTREACH = os.path.join(ROOT, "outreach")
TARGETS = os.path.join(OUTREACH, "targets.json")
LOG = os.path.join(OUTREACH, "outreach_log.jsonl")
MARKER = os.path.join(ROOT, "experiments", "results", "first_external.json")
REGISTRY = "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.AgentTanuki/agent-guild"


def _load():
    with open(TARGETS) as f:
        return json.load(f)


def _get(url, timeout=20):
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, headers={"accept": "application/json"}), timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
        return None


def _external_signal():
    """Read the genuine-external-agent detector's marker, if present."""
    if os.path.exists(MARKER):
        try:
            m = json.load(open(MARKER))
            if m.get("detected"):
                return "DETECTED", m
            return ("none", m)
        except (OSError, ValueError):
            pass
    return ("unknown", None)


def cmd_status(data):
    ts = data["targets"]
    by = {}
    for t in ts:
        by.setdefault(t["status"], []).append(t)
    print("=== Agent Guild outreach pipeline ===")
    for st in ("done", "pending", "blocked_needs_human"):
        items = by.get(st, [])
        print(f"\n[{st}] ({len(items)})")
        for t in items:
            ev = f" -> {t['evidence']}" if t.get("evidence") else ""
            print(f"  - {t['name']} ({t['method']}){ev}")
    sig, meta = _external_signal()
    print("\n=== Success metric: first GENUINE external agent ===")
    if sig == "DETECTED":
        print("  🎉 DETECTED. Distribution produced a real external agent. See:", json.dumps(meta.get("new_nonours_agents", []))[:200])
    elif sig == "none":
        mcp = (meta or {}).get("mcp_remote_calls_unattributable", 0)
        print(f"  ⏳ Not yet. (Unattributable MCP calls, incl. our own tests, not counted: {mcp}.)")
        print("  Listings exist; no third-party agent we can attribute has used the service.")
    else:
        print("  ? detector has not run yet; run live/scripts/detect_external.py")
    print("\nReminder: a listing count is not success. The detector flipping to DETECTED is.")


def cmd_next(data):
    print("=== Next actionable outreach targets (not blocked on an account) ===")
    copy = data["canonical_copy"]
    actionable = [t for t in data["targets"] if t["status"] == "pending"]
    if not actionable:
        print("  (none pending that aren't account-gated; see `status` for blocked items)")
    for t in actionable:
        print(f"\n## {t['name']}  [{t['method']}]  {t['url']}")
        print(f"   notes: {t.get('notes','')}")
        if t["method"] == "github_pr":
            print("   entry to add:")
            print("   " + copy["awesome_entry"])
    print("\n--- canonical copy ---")
    print(json.dumps(copy, indent=2))


def cmd_check(data):
    print("=== Live footprint check ===")
    r = _get(REGISTRY)
    if r and r.get("metadata", {}).get("count", 0) >= 1:
        s = r["servers"][0]["server"]
        meta = r["servers"][0].get("_meta", {}).get("io.modelcontextprotocol.registry/official", {})
        print(f"  official MCP registry: LISTED  name={s['name']}  status={meta.get('status')}")
    else:
        print("  official MCP registry: NOT FOUND (unexpected — investigate)")


def cmd_log(args):
    if len(args) < 2:
        print("usage: outreach.py log TARGET_ID STATUS [EVIDENCE]", file=sys.stderr)
        return 1
    rec = {"at": datetime.now(timezone.utc).isoformat(),
           "target_id": args[0], "status": args[1],
           "evidence": args[2] if len(args) > 2 else None}
    os.makedirs(OUTREACH, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print("logged:", json.dumps(rec))
    return 0


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "log":
        return cmd_log(args[1:])
    data = _load()
    if cmd == "status":
        cmd_status(data)
    elif cmd == "next":
        cmd_next(data)
    elif cmd == "check":
        cmd_check(data)
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
