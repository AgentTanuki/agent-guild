#!/usr/bin/env python3
"""Official MCP Registry readback gate.

Publication is not real until the OFFICIAL registry serves the exact release
back. This module fetches `GET /v0.1/servers/{name}/versions/{version}` (the
stable exact-version endpoint — never the substring `?search=`) and verifies
that the served record matches the local `server.json` on every
identity-bearing field:

  * exact server name (case-sensitive),
  * exact version,
  * exact repository URL,
  * exact MCP remote URL (type + url),
  * the publisher-provided trust block
    (`_meta["io.modelcontextprotocol.registry/publisher-provided"]
     ["ai.agent-guild/trust"]`) is served back — the registry stores and
    returns publisher-provided metadata (verified live 2026-07-14, e.g.
    io.github.06ketan/medium-ops), so its absence is a real failure, not
    a registry limitation.

The parser is pure (verify_readback) so it is unit-testable against legacy,
successful, missing-version and malformed registry responses:
live/guild/tests/test_registry_readback.py.

CLI exit codes (the workflow gates on 0):
  0 served and exact           2 served but MISMATCHED (wrong repo/remote/meta)
  1 never served (timeout)     3 malformed/unparseable registry response
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "https://registry.modelcontextprotocol.io"
PUBLISHER_PROVIDED = "io.modelcontextprotocol.registry/publisher-provided"
TRUST_KEY = "ai.agent-guild/trust"


class ReadbackResult:
    __slots__ = ("status", "reasons")

    def __init__(self, status: str, reasons: list[str]):
        self.status = status          # "ok" | "not_found" | "mismatch" | "malformed"
        self.reasons = reasons

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _served_server(body) -> dict | None:
    """Extract the ServerJSON from a detail response. Current shape nests it
    under "server"; tolerate a legacy flat shape where the fields are
    top-level. Returns None if neither shape is present."""
    if not isinstance(body, dict):
        return None
    srv = body.get("server")
    if isinstance(srv, dict) and "name" in srv:
        return srv
    if "name" in body and "version" in body:   # legacy flat shape
        return body
    return None


def verify_readback(body, expected: dict) -> ReadbackResult:
    """Pure verification of one registry detail response against the local
    server.json (`expected`). No network."""
    if isinstance(body, dict) and body.get("status") == 404:
        return ReadbackResult("not_found", ["registry: version not found"])
    srv = _served_server(body)
    if srv is None:
        return ReadbackResult(
            "malformed", ["unrecognised registry response shape "
                          f"(keys={sorted(body) if isinstance(body, dict) else type(body).__name__})"])

    reasons: list[str] = []
    if srv.get("name") != expected["name"]:
        reasons.append(f"name: served {srv.get('name')!r} != {expected['name']!r}")
    if srv.get("version") != expected["version"]:
        reasons.append(f"version: served {srv.get('version')!r} != {expected['version']!r}")
    exp_repo = expected["repository"]["url"]
    got_repo = (srv.get("repository") or {}).get("url")
    if got_repo != exp_repo:
        reasons.append(f"repository.url: served {got_repo!r} != {exp_repo!r}")

    exp_remotes = {(r.get("type"), r.get("url")) for r in expected.get("remotes", [])}
    got_remotes = {(r.get("type"), r.get("url")) for r in (srv.get("remotes") or [])}
    if not exp_remotes <= got_remotes:
        reasons.append(f"remotes: served {sorted(got_remotes)} is missing "
                       f"{sorted(exp_remotes - got_remotes)}")

    exp_trust = (expected.get("_meta") or {}).get(PUBLISHER_PROVIDED, {}).get(TRUST_KEY)
    if exp_trust is not None:
        got_trust = ((srv.get("_meta") or {}).get(PUBLISHER_PROVIDED) or {}).get(TRUST_KEY)
        if got_trust != exp_trust:
            reasons.append("publisher-provided trust _meta not served back "
                           "exactly" if got_trust else
                           "publisher-provided trust _meta missing from readback")

    return ReadbackResult("mismatch" if reasons else "ok", reasons)


def fetch_version(base: str, name: str, version: str, timeout: float = 30.0):
    """One GET of the exact-version endpoint. Returns (parsed_json | None,
    error_str | None). A 404 returns its parsed problem+json body."""
    url = (base.rstrip("/") + "/v0.1/servers/"
           + urllib.parse.quote(name, safe="") + "/versions/"
           + urllib.parse.quote(version, safe=""))
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "agent-guild-readback"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode()), None
        except Exception:
            return None, f"HTTP {e.code} with unparseable body"
    except json.JSONDecodeError as e:
        return None, f"invalid JSON from registry: {e}"
    except Exception as e:
        return None, f"fetch failed: {e}"


def poll(expected: dict, base: str = DEFAULT_BASE, attempts: int = 30,
         interval: float = 10.0) -> int:
    """Poll until the exact release is served. Bounded; distinguishes
    'never appeared' from 'appeared but wrong'."""
    last = None
    for attempt in range(1, attempts + 1):
        body, err = fetch_version(base, expected["name"], expected["version"])
        if err:
            print(f"attempt {attempt}/{attempts}: {err}")
            last = ReadbackResult("malformed", [err])
        else:
            last = verify_readback(body, expected)
            if last.ok:
                print(f"readback OK: {expected['name']}@{expected['version']} "
                      "served with exact name/version/repository/remote + trust _meta")
                return 0
            if last.status == "mismatch":
                # served, but wrong — no amount of waiting fixes identity drift
                print("::error::registry serves the version but it MISMATCHES:")
                for r in last.reasons:
                    print(f"::error::  {r}")
                return 2
            print(f"attempt {attempt}/{attempts}: {last.status} — "
                  + "; ".join(last.reasons))
        if attempt < attempts:
            time.sleep(interval)
    if last is not None and last.status == "malformed":
        print("::error::registry responses never parsed cleanly")
        return 3
    print(f"::error::registry never served {expected['name']}@{expected['version']}")
    return 1


def main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--server-json", default="server.json")
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--attempts", type=int, default=30)
    p.add_argument("--interval", type=float, default=10.0)
    a = p.parse_args(argv)
    expected = json.loads(open(a.server_json).read())
    return poll(expected, base=a.base, attempts=a.attempts, interval=a.interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
