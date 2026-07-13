#!/usr/bin/env python3
"""Orchestrate ONE live machine-only market run against production, and capture
the baseline comparison (Guild-selected vs unassisted delegation).

Zero human steps. It:
  1. wakes the free-plan worker (inbound GET) and waits for it to be routable
     via the PUBLIC /check routing gate (it re-verifies its own endpoint);
     as a deterministic backstop it triggers a Guild-observed invocation
  2. runs the Node buyer in `guild` mode (full loop) and in `unassisted` mode
     (naive baseline) and records both metric objects
  3. writes live/market_clients/last_run.json

Discovery between buyer and worker is ONLY through the Guild's public
interfaces. The worker is honestly first-party demo supply.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

GUILD = os.environ.get("GUILD_URL", "https://agent-guild-5d5r.onrender.com").rstrip("/")
WORKER = os.environ.get("WORKER_URL", "https://agent-guild-market-worker.onrender.com").rstrip("/")
CAP = "text.stats"
HERE = pathlib.Path(__file__).resolve().parent
TOKEN_FILE = HERE.parent / "secrets" / "first_party_token"


def _fp_headers() -> dict:
    # shared helper: Guild-operated traffic ALWAYS tags first-party (the old
    # copy returned {} without a token and silently counted as external)
    sys.path.insert(0, str(HERE))
    from _firstparty import firstparty_headers
    return firstparty_headers(role="test")


def _get(url: str, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "market-orchestrator/1",
                                               **_fp_headers()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def wake_worker() -> dict:
    for i in range(20):
        try:
            info = _get(WORKER + "/", timeout=30)
            print(f"[orch] worker awake: agent {info.get('agent_id')}", flush=True)
            return info
        except Exception as e:
            print(f"[orch] waking worker ({i}): {e}", flush=True)
            time.sleep(15)
    raise SystemExit("worker never woke")


def wait_routable(timeout_s=600) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        check = _get(f"{GUILD}/check?capability={CAP}&cb={int(time.time())}")
        routing = check.get("routing") or {}
        if routing.get("routable"):
            print(f"[orch] ROUTABLE via gate: {routing['provider_id']} "
                  f"({routing['reachability_status']})", flush=True)
            return routing
        # keep the worker awake + nudge re-verification
        try:
            _get(WORKER + "/", timeout=20)
        except Exception:
            pass
        print(f"[orch] not routable yet: {routing.get('reason')}", flush=True)
        time.sleep(15)
    raise SystemExit("worker never became routable via the public gate")


def run_buyer(mode: str) -> dict:
    env = dict(os.environ)
    env.update(_env_token())
    proc = subprocess.run(
        ["node", str(HERE / "buyer" / "buyer.mjs"), mode],
        capture_output=True, text=True, env=env, timeout=1800)
    print(proc.stderr[-2000:], flush=True)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"mode": mode, "success": False,
                "error": "buyer produced no JSON", "stdout": proc.stdout[-1000:]}


def _env_token() -> dict:
    tok = os.environ.get("GUILD_FIRST_PARTY_TOKEN", "").strip()
    if not tok and TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text().strip()
    return {"GUILD_FIRST_PARTY_TOKEN": tok} if tok else {}


def main() -> int:
    wake_worker()
    routing = wait_routable()
    print("[orch] === GUILD-SELECTED RUN ===", flush=True)
    guild = run_buyer("guild")
    print("[orch] === UNASSISTED BASELINE RUN ===", flush=True)
    unassisted = run_buyer("unassisted")
    out = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "guild": GUILD, "worker": WORKER, "capability": CAP,
           "routing_at_start": routing,
           "guild_selected": guild, "unassisted": unassisted}
    (HERE / "last_run.json").write_text(json.dumps(out, indent=1))
    print(f"[orch] guild.success={guild.get('success')} "
          f"unassisted.success={unassisted.get('success')}", flush=True)
    print(f"[orch] wrote {HERE / 'last_run.json'}", flush=True)
    return 0 if guild.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
