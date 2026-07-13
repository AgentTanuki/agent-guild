#!/usr/bin/env python3
"""Backup → canary → rollback deployment verifier for Agent Guild activations.

Usage:
  python live/scripts/deploy_canary.py baseline     # BEFORE deploy: snapshot prod
  python live/scripts/deploy_canary.py verify       # AFTER deploy: canary checks
  python live/scripts/deploy_canary.py rollback <commit>   # auto-revert + push

The activation itself is declarative (render.yaml env: GUILD_STORE=sqlite,
GUILD_HASH_KEYS=1, GUILD_ABUSE_CONTROLS=1) and deploys on git push. The
persistent-disk JSON file is the rollback artifact: the app snapshots it to
guild.json.pre-sqlite-<ts> before first migration and never writes it again
under sqlite, so reverting the env change restores the pre-cutover world.

`verify` exits non-zero if ANY canary fails — wire it to `rollback` in CI or
run rollback manually with the activation commit hash.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

HOST = os.environ.get("GUILD_HOST", "https://agent-guild-5d5r.onrender.com")
REPO = pathlib.Path(__file__).resolve().parents[2]
BASELINE = REPO / "live" / "scripts" / "deploy_baseline.json"


def _get(path: str) -> dict:
    req = urllib.request.Request(HOST + path, headers={
        "User-Agent": "guild-deploy-canary", "X-Guild-Source": "guild-ci"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode())


def baseline() -> int:
    snap = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "health": _get("/health"),
        "ledger_stats": _get("/ledger/stats"),
        "checkpoint": _get("/ledger/checkpoint")["checkpoint"],
    }
    BASELINE.write_text(json.dumps(snap, indent=1))
    print(f"baseline written: {BASELINE}")
    print(json.dumps(snap["health"], indent=1))
    return 0


def verify(expect_store: str = "sqlite") -> int:
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = ""):
        print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else None
    h = _get("/health")
    check("health ok", h.get("ok") is True)
    check(f"store == {expect_store}", h.get("store") == expect_store,
          f"store={h.get('store')}")
    check("hashed_keys active", h.get("hashed_keys") is True)
    check("abuse_controls active", h.get("abuse_controls") is True)
    if os.environ.get("GUILD_EXPECT_STRICT_FIRST_PARTY") == "1":
        check("strict_first_party active", h.get("strict_first_party") is True)
    st = _get("/ledger/stats")
    check("chain_valid", st.get("chain_valid") is True)
    rec = _get("/ledger/reconcile")
    check("reconcile clean", rec.get("clean") is True,
          f"mismatches={len(rec.get('mismatches', []))}")
    check("no one-party records at guild_mediated",
          st.get("by_provenance", {}).get("guild_mediated", 0)
          <= st.get("by_provenance_original", {}).get("guild_mediated", 10**9))
    if base:
        for k in ("agents", "tasks", "attestations"):
            check(f"no data loss: {k}", h.get(k, -1) >= base["health"].get(k, 0),
                  f"live={h.get(k)} baseline={base['health'].get(k)}")
        check("ledger did not shrink",
              st.get("records", -1) >= base["ledger_stats"].get("records", 0))
    issuer = _get("/ledger/issuer")
    check("issuer continuity", issuer.get("continuity_valid") is True)
    print("\n" + ("CANARY CLEAN" if not fails else "CANARY FAILED: " + ", ".join(fails)))
    return 0 if not fails else 1


def rollback(commit: str) -> int:
    """Automated rollback: revert the activation commit and push — Render
    redeploys with the previous env; the untouched JSON file serves again."""
    subprocess.run(["git", "-C", str(REPO), "revert", "--no-edit", commit],
                   check=True)
    subprocess.run(["git", "-C", str(REPO), "push", "origin", "main"], check=True)
    print(f"reverted {commit} and pushed — Render will redeploy the previous config")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if cmd == "baseline":
        sys.exit(baseline())
    if cmd == "verify":
        sys.exit(verify(os.environ.get("GUILD_EXPECT_STORE", "sqlite")))
    if cmd == "rollback":
        sys.exit(rollback(sys.argv[2]))
    print(__doc__)
    sys.exit(2)
