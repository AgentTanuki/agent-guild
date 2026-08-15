#!/usr/bin/env python3
"""Official MCP Registry readback gate.

Publication is not real until the OFFICIAL registry serves the exact release
back. This module first fetches the stable exact-version endpoint
`GET /v0.1/servers/{name}/versions/{version}` and verifies that the served
record matches the local `server.json` on every
identity-bearing field:

  * exact server name (case-sensitive),
  * exact version,
  * exact publisher-authored description and website URL,
  * expected repository fields,
  * exact authored MCP remotes (including headers or variables),
  * the complete publisher-provided metadata block is served back exactly.

The official envelope's own metadata is allowed to differ, but no authored
buyer-facing field may be stripped from an immutable Registry version.
Optional `--search` checks run only after that exact proof; search is evidence
of findability, never a substitute for exact publication readback.

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
    for field in ("description", "websiteUrl", "title", "icons", "packages"):
        if field in expected and srv.get(field) != expected[field]:
            reasons.append(
                f"{field}: served {srv.get(field)!r} != {expected[field]!r}")
    exp_repo = expected.get("repository") or {}
    got_repo = srv.get("repository") or {}
    for field, value in exp_repo.items():
        if got_repo.get(field) != value:
            reasons.append(
                f"repository.{field}: served {got_repo.get(field)!r} != {value!r}")

    exp_remotes = expected.get("remotes") or []
    got_remotes = srv.get("remotes") or []
    if got_remotes != exp_remotes:
        reasons.append(
            f"remotes: served {got_remotes!r} != {exp_remotes!r}")

    exp_publisher = (expected.get("_meta") or {}).get(PUBLISHER_PROVIDED)
    if exp_publisher is not None:
        got_publisher = (srv.get("_meta") or {}).get(PUBLISHER_PROVIDED)
        if got_publisher != exp_publisher:
            reasons.append(
                "publisher-provided _meta not served back exactly"
                if got_publisher else
                "publisher-provided _meta missing from readback"
            )

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


def fetch_search(base: str, query: str, timeout: float = 30.0):
    """One official name-substring search; same result/error contract."""
    url = (base.rstrip("/") + "/v0.1/servers?" + urllib.parse.urlencode({
        "search": query, "version": "latest", "limit": 100,
    }))
    req = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": "agent-guild-readback",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode()), None
    except json.JSONDecodeError as exc:
        return None, f"invalid search JSON: {exc}"
    except Exception as exc:
        return None, f"search fetch failed: {exc}"


def search_contains(body, expected_name: str) -> bool:
    """Pure parser for the official ServerListResponse shape."""
    if not isinstance(body, dict) or not isinstance(body.get("servers"), list):
        return False
    return any(
        (_served_server(item) or {}).get("name") == expected_name
        for item in body["servers"]
    )


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
                      "served with exact publisher-authored discovery fields")
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


def poll_searches(expected_name: str, queries: list[str], *,
                  base: str = DEFAULT_BASE, attempts: int = 30,
                  interval: float = 10.0) -> int:
    """Prove the published name is returned for every intended buyer term."""
    remaining = list(dict.fromkeys(queries))
    for attempt in range(1, attempts + 1):
        missing = []
        for query in remaining:
            body, error = fetch_search(base, query)
            if error or not search_contains(body, expected_name):
                missing.append(query)
        if not missing:
            print("search readback OK: " + expected_name + " appears for "
                  + ", ".join(queries))
            return 0
        remaining = missing
        print(f"search attempt {attempt}/{attempts}: missing {missing}")
        if attempt < attempts:
            time.sleep(interval)
    print(f"::error::registry search never returned {expected_name} for "
          + ", ".join(remaining))
    return 1


def main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--server-json", default="server.json")
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--attempts", type=int, default=30)
    p.add_argument("--interval", type=float, default=10.0)
    p.add_argument("--search", action="append", default=[])
    a = p.parse_args(argv)
    expected = json.loads(open(a.server_json).read())
    exact = poll(expected, base=a.base, attempts=a.attempts,
                 interval=a.interval)
    if exact != 0 or not a.search:
        return exact
    return poll_searches(
        expected["name"], a.search, base=a.base,
        attempts=a.attempts, interval=a.interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
