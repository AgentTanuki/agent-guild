"""Endpoint + metadata hardening regression tests.

These lock in two things that previously broke MCP scanners and eroded trust:

  1. The bare ``/mcp`` path must resolve directly — no 307 redirect (which some
     clients won't follow on POST and which is fragile behind a TLS-terminating
     proxy). Both ``/mcp`` and ``/mcp/`` must serve the MCP server.
  2. Every surface (FastAPI app, ``/`` JSON, the public manifest, and the MCP
     ``serverInfo``) must report the SAME version, sourced from ``app.__version__``.
"""
import json

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


def test_focused_mcp_bare_and_trailing_slash_both_initialize():
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "focused-regression", "version": "1.0"},
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(app) as client:
        for path in ("/mcp/payment-safety", "/mcp/payment-safety/"):
            response = client.post(
                path, headers=headers, json=payload, follow_redirects=False)
            assert response.status_code == 200, (path, response.text)
            assert response.status_code != 307
            init = json.loads(next(
                line[5:].strip() for line in response.text.splitlines()
                if line.startswith("data:")))
            assert init["result"]["serverInfo"]["name"] == \
                "Agent Guild x402 Payment Safety"
            session = response.headers["mcp-session-id"]
            session_headers = {**headers, "Mcp-Session-Id": session}
            notified = client.post(path, headers=session_headers, json={
                "jsonrpc": "2.0", "method": "notifications/initialized",
                "params": {},
            })
            assert notified.status_code == 202
            listed = client.post(path, headers=session_headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/list",
                "params": {},
            })
            result = json.loads(next(
                line[5:].strip() for line in listed.text.splitlines()
                if line.startswith("data:")))
            assert [tool["name"] for tool in result["result"]["tools"]] == [
                "guild_x402_payment_safety"]


def test_focused_mcp_unpaid_call_returns_complete_request_bound_challenge(
        monkeypatch):
    from app import paymentdecision, payments

    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv(
        "GUILD_X402_PAY_TO", "0x1111111111111111111111111111111111111111")
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    arguments = {
        "payment": {
            "scheme": "exact",
            "network": "eip155:8453",
            "asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            "amount": "25000",
            "pay_to": "0x" + "33" * 20,
            "resource": "https://seller.example/research/42",
        },
        "capability": "fact-check",
        "policy": {"max_risk": 32.99, "min_confidence": 0.5},
        "ttl_seconds": 300,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    def sse(response):
        return json.loads(next(
            line[5:].strip() for line in response.text.splitlines()
            if line.startswith("data:")))

    with TestClient(app) as client:
        initialized = client.post(
            "/mcp/payment-safety/", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "call-regression", "version": "1"},
                },
            })
        session_headers = {
            **headers, "Mcp-Session-Id": initialized.headers["mcp-session-id"]}
        client.post("/mcp/payment-safety/", headers=session_headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
            "params": {},
        })
        called = client.post(
            "/mcp/payment-safety/", headers=session_headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {
                    "name": "guild_x402_payment_safety",
                    "arguments": arguments,
                },
            })

    result = sse(called)["result"]
    assert result["isError"] is True
    challenge = result["structuredContent"]
    expected = payments.payment_decision_request(
        paymentdecision.request_sha256(arguments))
    assert challenge["x402Version"] == 2
    assert challenge["resource"]["url"] == expected.resource_url
    assert "credentialSubject" not in challenge
    assert set(challenge["accepts"][0]) >= {
        "scheme", "network", "asset", "amount", "payTo",
        "maxTimeoutSeconds",
    }


def test_version_is_consistent_across_surfaces():
    with TestClient(app) as client:
        assert app.version == __version__
        root = client.get("/", headers={"accept": "application/json"}).json()
        assert root["version"] == __version__
        manifest = client.get("/.well-known/agent-guild.json").json()
        assert manifest["version"] == __version__
