"""Regression: the production /mcp surface must accept the public Host.

2026-07-10: an unpinned fastmcp release enabled its Host/Origin guard with
localhost-only defaults, so EVERY external MCP request — including the
initialize the official MCP registry entry points clients at — got a bare
`421 Misdirected Request`. Initialisation, tool discovery and invocation were
all dead, silently, while /health stayed green.

These tests replay the exact failing request against the ASGI app with the
production Host header, and prove the guard still REJECTS unknown hosts (the
fix is a narrow allowlist, not a global disable).
Evidence: docs/discovery-swarm/evidence/mcp-421-host-guard.md
"""
import os, tempfile

os.environ.setdefault("GUILD_DATA", os.path.join(tempfile.mkdtemp(), "g.json"))
os.environ.setdefault("GUILD_BOOTSTRAP_EVAL", "0")

from fastapi.testclient import TestClient
from app.main import app
from app.mcp_server import PUBLIC_HOSTS

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "pilot-a-audit", "version": "1"}}}
HDRS = {"Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"}


def _post_init(host):
    # lifespan must run so the MCP session manager is live
    with TestClient(app) as c:
        return c.post("/mcp/", json=INIT, headers={**HDRS, "host": host})


def test_public_host_initialize_succeeds():
    """The exact request that failed in production on 2026-07-10."""
    r = _post_init(PUBLIC_HOSTS[0])
    assert r.status_code == 200, r.text
    assert '"protocolVersion"' in r.text


def test_localhost_initialize_succeeds():
    r = _post_init("localhost")
    assert r.status_code == 200, r.text


def test_unknown_host_is_rejected_421():
    """The allowlist must actually guard: a global disable would pass any
    Host and silently drop the defense this fix is supposed to keep."""
    r = _post_init("evil.example")
    assert r.status_code == 421, (r.status_code, r.text[:100])
