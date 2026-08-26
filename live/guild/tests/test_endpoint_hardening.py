"""Endpoint + metadata hardening regression tests.

These lock in two things that previously broke MCP scanners and eroded trust:

  1. The bare ``/mcp`` path must resolve directly — no 307 redirect (which some
     clients won't follow on POST and which is fragile behind a TLS-terminating
     proxy). Both ``/mcp`` and ``/mcp/`` must serve the MCP server.
  2. Every surface (FastAPI app, ``/`` JSON, the public manifest, and the MCP
     ``serverInfo``) must report the SAME version, sourced from ``app.__version__``.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.main import app
from app.store import Store


@pytest.fixture
def private_demand_store(monkeypatch, tmp_path):
    """Exercise a real demand write without touching the suite-wide Store.

    The paid-wrapper regression intentionally exercises ``guild_check`` all
    the way through its pre-authorization demand recorder.  Demand, dedupe and
    runner state are write-through under SQLite, so truncating only the shared
    in-memory view would be false isolation.  Instead, inject a fresh Store of
    the active backend type into every module reference used by this route.
    """
    from app import main as main_module
    from app import mcp_server as mcp_module
    from app import state as state_module

    mode = state_module.store.store_mode
    monkeypatch.setenv("GUILD_STORE", mode)
    if mode == "sqlite":
        monkeypatch.setenv(
            "GUILD_STORE_PATH", str(tmp_path / "trust-test.sqlite"))
        monkeypatch.setenv("GUILD_DATA", "")
        private = Store()
    else:
        monkeypatch.delenv("GUILD_STORE_PATH", raising=False)
        monkeypatch.setenv("GUILD_DATA", str(tmp_path / "trust-test.json"))
        private = Store()

    monkeypatch.setattr(state_module, "store", private)
    monkeypatch.setattr(main_module, "store", private)
    monkeypatch.setattr(mcp_module, "store", private)
    yield private
    assert private.state_diagnostics()["divergence"] == []


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


def test_sessionless_tools_list_has_read_only_discovery_compatibility():
    """Metadata-only crawlers can inspect tools without opening a session."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(app) as client:
        for path in ("/mcp", "/mcp/"):
            response = client.post(path, headers=headers, json={
                "jsonrpc": "2.0", "id": 7, "method": "tools/list",
                "params": {},
            })
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(
                "application/json")
            body = response.json()
            assert body["jsonrpc"] == "2.0"
            assert body["id"] == 7
            assert any(tool["name"] == "guild_check"
                       for tool in body["result"]["tools"])


def test_sessionless_compatibility_does_not_allow_tool_calls():
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "guild_check", "arguments": {}},
            },
        )
    assert response.status_code == 400
    assert response.json()["error"]["message"] == \
        "Bad Request: Missing session ID"


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


def test_trust_mcp_bare_and_trailing_slash_expose_only_trust_reads():
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "trust-regression", "version": "1.0"},
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    expected = [
        "guild_preflight",
        "guild_index",
        "guild_preflight_deep",
        "guild_check",
        "guild_search",
        "guild_best_agent",
        "guild_risk_score",
        "guild_passport",
        "guild_verify",
    ]
    with TestClient(app) as client:
        for path in ("/mcp/trust", "/mcp/trust/"):
            response = client.post(
                path, headers=headers, json=payload, follow_redirects=False)
            assert response.status_code == 200, (path, response.text)
            assert response.status_code != 307
            init = json.loads(next(
                line[5:].strip() for line in response.text.splitlines()
                if line.startswith("data:")))
            assert init["result"]["serverInfo"]["name"] == \
                "Agent Guild Trust Reads"
            session_headers = {
                **headers,
                "Mcp-Session-Id": response.headers["mcp-session-id"],
            }
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
            assert [tool["name"] for tool in result["result"]["tools"]] == \
                expected
            for hidden_name in (
                    "guild_register", "guild_envelope_issue",
                    "guild_coordination_policy"):
                called = client.post(path, headers=session_headers, json={
                    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": hidden_name, "arguments": {}},
                })
                hidden = json.loads(next(
                    line[5:].strip() for line in called.text.splitlines()
                    if line.startswith("data:")))
                outcome = hidden["result"]
                assert outcome["isError"] is True
                assert outcome["content"][0]["text"] == \
                    f"Unknown tool: '{hidden_name}'"


def test_trust_mcp_paid_wrapper_preserves_standard_x402_challenge(
        monkeypatch, private_demand_store):
    from app import payments

    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv(
        "GUILD_X402_PAY_TO", "0x1111111111111111111111111111111111111111")
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
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
            "/mcp/trust/", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "trust-paid", "version": "1"},
                },
            })
        session_headers = {
            **headers,
            "Mcp-Session-Id": initialized.headers["mcp-session-id"],
        }
        client.post("/mcp/trust/", headers=session_headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
            "params": {},
        })
        called = client.post(
            "/mcp/trust/", headers=session_headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {
                    "name": "guild_check",
                    "arguments": {"capability": "fact-check"},
                },
            })

    outcome = sse(called)["result"]
    assert outcome["isError"] is True
    challenge = outcome["structuredContent"]
    expected = payments.check_request("fact-check")
    assert challenge["x402Version"] == 2
    assert challenge["resource"]["url"] == expected.resource_url
    assert len(challenge["accepts"]) == 1


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
