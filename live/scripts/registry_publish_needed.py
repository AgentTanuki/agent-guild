#!/usr/bin/env python3
"""Decide whether the official MCP Registry still needs ``server.json``.

This is the recovery-side complement to ``registry_readback.py``.  A release
can reach production after the original ship gate times out, or a registry
dispatch can be swallowed by GitHub's ``GITHUB_TOKEN`` recursion guard.  In
either case, looking only at whether *this PR* changed ``server.json`` leaves
an already-merged registry version stranded forever.

The decision is deliberately exact-version and fail-closed:

* 404 for the local name+version -> publication is needed;
* an exact, byte-semantic readback match -> already published;
* malformed, unreachable, or same-version-but-different metadata -> error.

It never guesses from a search result and never republishes an immutable
version whose served metadata differs from the local release.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import registry_readback


def decide(body, expected: dict) -> dict:
    """Pure publication decision from one exact-version registry response."""
    result = registry_readback.verify_readback(body, expected)
    if result.ok:
        return {
            "needed": False,
            "state": "already_current",
            "name": expected["name"],
            "version": expected["version"],
        }
    if result.status == "not_found":
        return {
            "needed": True,
            "state": "version_not_found",
            "name": expected["name"],
            "version": expected["version"],
        }
    raise ValueError(
        "registry publication state is unsafe: " + result.status + ": "
        + "; ".join(result.reasons)
    )


def _write_github_output(path: str, decision: dict) -> None:
    target = pathlib.Path(path)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(f"needed={'true' if decision['needed'] else 'false'}\n")
        fh.write(f"state={decision['state']}\n")
        fh.write(f"version={decision['version']}\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-json", default="server.json")
    parser.add_argument("--base", default=registry_readback.DEFAULT_BASE)
    parser.add_argument("--github-output")
    args = parser.parse_args(argv)

    expected = json.loads(pathlib.Path(args.server_json).read_text())
    body, error = registry_readback.fetch_version(
        args.base, expected["name"], expected["version"]
    )
    if error:
        print(f"::error::cannot determine registry publication state: {error}")
        return 3
    try:
        decision = decide(body, expected)
    except ValueError as exc:
        print(f"::error::{exc}")
        return 2

    print(json.dumps(decision, sort_keys=True))
    if args.github_output:
        _write_github_output(args.github_output, decision)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
