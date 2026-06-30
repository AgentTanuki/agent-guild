"""Endpoint + metadata hardening regression tests.

These lock in two things that previously broke MCP scanners and eroded trust:

  1. The bare ``/mcp`` path must resolve directly — no 307 redirect (which some
     clients won't follow on POST and which is fragile behind a TLS-terminating
     proxy). Both ``/mcp`` and ``/mcp/`` must serve the MCP server.
  2. Every surface (FastAPI app, ``/`` JSON, the public manifest, and the MCP
     ``serverInfo``) must report the SAME version, sourced from ``app.__version__``.
"""
from fastapi.testclient import TestClient

from app import __version__
from app.main import app


def test_bare_mcp_does_not_redirect():
    """POST /mcp (no trailing slash) must NOT 307; it must reach the MCP app."""
    with TestClient(app) as client:
        r = client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "regression", "version": "1.0"},
                },
            },
            follow_redirects=False,
        )
    assert r.status_code != 307, "bare /mcp still issues a redirect"
    assert r.status_code == 200, f"bare /mcp returned {r.status_code}"


def test_mcp_trailing_slash_also_works():
    with TestClient(app) as client:
        r = client.post(
            "/mcp/",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "regression", "version": "1.0"},
                },
            },
            follow_redirects=False,
        )
    assert r.status_code == 200


def test_version_is_consistent_across_surfaces():
    with TestClient(app) as client:
        assert app.version == __version__
        root = client.get("/", headers={"accept": "application/json"}).json()
        assert root["version"] == __version__
        manifest = client.get("/.well-known/agent-guild.json").json()
        assert manifest["version"] == __version__
