#!/usr/bin/env python3
"""Publish the current ledger checkpoint to INDEPENDENTLY CONTROLLED locations.

A checkpoint pinned only on the service's own disk protects nobody from the
service. This script makes the commitment external:

  1. POST /ledger/checkpoint/publish            (seal the current head)
  2. write docs/checkpoints/checkpoint-<i>.json (git repo → GitHub, location 1)
  3. request an Internet Archive snapshot of the live /ledger/checkpoints feed
     (web.archive.org, location 2 — controlled by the Internet Archive, not us)

Run after each meaningful batch of evidence (the ops-watch scheduled task calls
it daily). Requires GUILD_ADMIN_TOKEN in the env or live/secrets/guild_admin_token.

Independence claims, honestly stated:
  * GitHub copy — controlled by the AgentTanuki GitHub account (separate
    infrastructure from Render, but the same operator).
  * Internet Archive snapshot — controlled by archive.org (neither our
    infrastructure nor our operator; genuinely independent).
Anyone else can (and is encouraged to) pin /ledger/checkpoint themselves.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import urllib.request

HOST = os.environ.get("GUILD_HOST", "https://agent-guild-5d5r.onrender.com")
REPO = pathlib.Path(__file__).resolve().parents[2]
OUTDIR = REPO / "docs" / "checkpoints"


def _admin_token() -> str:
    tok = os.environ.get("GUILD_ADMIN_TOKEN", "").strip()
    if tok:
        return tok
    p = REPO / "live" / "secrets" / "guild_admin_token"
    if p.exists():
        return p.read_text().strip()
    raise SystemExit("no admin token (GUILD_ADMIN_TOKEN or live/secrets/guild_admin_token)")


def _post(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, method="POST", headers=headers, data=b"")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def main() -> int:
    # 1. seal + publish on the service
    entry = _post(f"{HOST}/ledger/checkpoint/publish",
                  {"X-Admin-Token": _admin_token()})["checkpoint"]
    idx = entry["index"]
    print(f"published checkpoint index={idx} head={entry['checkpoint']['head_hash'][:16]}…")

    # 2. pin in the git repo (→ GitHub on push)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    path = OUTDIR / f"checkpoint-{idx:05d}.json"
    path.write_text(json.dumps(entry, indent=1, sort_keys=True) + "\n")
    (OUTDIR / "latest.json").write_text(
        json.dumps(entry, indent=1, sort_keys=True) + "\n")
    try:
        subprocess.run(["git", "-C", str(REPO), "add", str(OUTDIR)], check=True)
        diff = subprocess.run(["git", "-C", str(REPO), "diff", "--cached",
                               "--quiet"]).returncode
        if diff != 0:
            subprocess.run(["git", "-C", str(REPO),
                            "-c", "user.name=AgentTanuki",
                            "-c", "user.email=agenttanuki@users.noreply.github.com",
                            "commit", "-q", "-m",
                            f"chore(ledger): pin checkpoint {idx}"], check=True)
            print(f"pinned in repo: {path.relative_to(REPO)} (push to publish on GitHub)")
    except subprocess.CalledProcessError as e:
        print(f"WARN: git pin failed: {e}", file=sys.stderr)

    # 3. independent snapshot at the Internet Archive
    try:
        _get(f"https://web.archive.org/save/{HOST}/ledger/checkpoints?limit=5")
        print("Internet Archive snapshot requested "
              f"(https://web.archive.org/web/*/{HOST}/ledger/checkpoints*)")
    except Exception as e:  # archive.org save can be slow/flaky; never fatal
        print(f"WARN: archive.org snapshot failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
