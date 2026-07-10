#!/usr/bin/env python3
"""First-party MCP canary — periodic production health of the /mcp surface.

Runs the full standards-compliant handshake plus host/origin-guard assertions
and reports latency. Identifies as AG_TEST (UA `mcp:guild-canary/1`, matched by
attribution.AG_TEST_UA_RE) and sends the first-party token, so its traffic can
NEVER count as genuine external activity. Intended for a scheduled task.

Exit 0 = all checks pass; 1 = a check failed (page the operator); 2 = usage.

Env: GUILD_BASE (default prod), GUILD_FIRST_PARTY_TOKEN (optional; also read
from live/secrets/first_party_token if present).
"""
from __future__ import annotations

import json, os, sys, time, urllib.request

BASE = os.environ.get("GUILD_BASE", "https://agent-guild-5d5r.onrender.com").rstrip("/")
UA = "mcp:guild-canary/1"          # AG_TEST by attribution rules
TIMEOUT = 15


def _token() -> str:
    t = os.environ.get("GUILD_FIRST_PARTY_TOKEN", "")
    if t:
        return t
    p = os.path.join(os.path.dirname(__file__), "..", "secrets", "first_party_token")
    try:
        return open(p).read().strip()
    except OSError:
        return "guild-canary"      # harmless when strict mode is off


def _post(path, body, extra=None):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("User-Agent", UA)
    req.add_header("X-Guild-Source", _token())
    for k, v in (extra or {}).items():
        req.add_header(k, v)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.headers, r.read().decode(), (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode()[:300], (time.time() - t0) * 1000
    except Exception as e:
        return 0, {}, f"ERR {e}", (time.time() - t0) * 1000


def _sse(raw):
    for line in raw.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return json.loads(raw)


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "guild-canary", "version": "1"}}}


def main():
    checks, failures = [], []

    def check(name, ok, detail=""):
        checks.append((name, ok, detail))
        if not ok:
            failures.append(name)

    st, hdrs, body, ms = _post("/mcp/", INIT)
    check("initialize", st == 200 and '"protocolVersion"' in body, f"HTTP {st} {ms:.0f}ms")
    sid = hdrs.get("Mcp-Session-Id") if hasattr(hdrs, "get") else None

    if sid:
        _post("/mcp/", {"jsonrpc": "2.0", "method": "notifications/initialized"},
              {"Mcp-Session-Id": sid})
        st, _, body, ms = _post("/mcp/", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                {"Mcp-Session-Id": sid})
        tools = []
        try:
            tools = [t["name"] for t in _sse(body)["result"]["tools"]]
        except Exception:
            pass
        check("tools_list", st == 200 and "guild_check" in tools, f"{len(tools)} tools {ms:.0f}ms")

        st, _, body, ms = _post("/mcp/", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                          "params": {"name": "ag_json_canonicalize",
                                                     "arguments": {"payload": {"value": {"b": 2, "a": 1}}}}},
                                {"Mcp-Session-Id": sid})
        ok_inv = False
        try:
            ok_inv = _sse(body)["result"].get("isError") is False
        except Exception:
            pass
        check("harmless_invoke", st == 200 and ok_inv, f"{ms:.0f}ms")

        st, _, body, ms = _post("/mcp/", {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                          "params": {"name": "ag_json_canonicalize",
                                                     "arguments": {"payload": {"nope": 1}}}},
                                {"Mcp-Session-Id": sid})
        check("structured_error", "error" in body.lower() and "jsonrpc" in body, f"HTTP {st}")
    else:
        check("session_id_present", False, "no Mcp-Session-Id header")

    # host guard: a spoofed Host must be rejected. Behind Render/Cloudflare an
    # unknown Host is rejected at the EDGE (403/404) before it reaches the
    # origin's 421 — any of those means "not served", which is the property we
    # care about. A 200 would mean the guard is off.
    st, _, _, _ = _post("/mcp/", INIT, {"Host": "canary-evil.example"})
    check("host_guard_active", st in (421, 403, 404), f"spoofed-host HTTP {st}")
    # foreign browser Origin must be rejected
    st, _, _, _ = _post("/mcp/", INIT, {"Origin": "https://canary-evil.example"})
    check("origin_guard_active", st == 403, f"foreign-origin HTTP {st}")

    out = {"base": BASE, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
           "failures": failures, "healthy": not failures}
    print(json.dumps(out, indent=1))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
