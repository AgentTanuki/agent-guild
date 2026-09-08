"""Authenticated MCP validation must not become external growth."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import attribution, firstparty, mcp_server, state
from app.store import Store


@pytest.fixture
def isolated_store(monkeypatch):
    from app import main
    store = Store(path="")
    for module in (main, mcp_server, state):
        monkeypatch.setattr(module, "store", store)
    monkeypatch.setenv("GUILD_FIRST_PARTY_TOKEN", "test-current-token")
    monkeypatch.setenv("GUILD_FIRST_PARTY_TOKEN_PREV", "test-previous-token")
    return store


def rpc(client, method, params, headers, request_id=1):
    response = client.post("/mcp/", headers={
        "Host": "localhost", "Accept": "application/json, text/event-stream",
        **headers,
    }, json={"jsonrpc": "2.0", "id": request_id,
             "method": method, "params": params})
    assert response.status_code == 200, response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        body = next(json.loads(line[6:]) for line in response.text.splitlines()
                    if line.startswith("data: ") and '"id"' in line)
    else:
        body = response.json()
    assert "error" not in body, body
    return response, body["result"]


@pytest.mark.parametrize("headers,expected", [
    ({"X-Agent-Guild-First-Party": "test-current-token",
      "X-Agent-Guild-Role": "test"}, "AG_TEST"),
    ({"X-Agent-Guild-First-Party": "test-previous-token"}, "AG_INTERNAL"),
    ({"X-Guild-Source": "test-current-token"}, "AG_INTERNAL"),
    ({"X-Agent-Guild-First-Party": "wrong", "X-Agent-Guild-Role": "test"}, None),
    ({}, None),
])
def test_real_http_tool_events_use_authenticated_headers(
        isolated_store, headers, expected):
    from app.main import app
    with TestClient(app) as client:
        initialized, _ = rpc(client, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "MCPClient", "version": "1.0.0"},
        }, headers)
        session = initialized.headers.get("mcp-session-id")
        transport_headers = {"MCP-Protocol-Version": "2025-03-26"}
        if session:
            transport_headers["Mcp-Session-Id"] = session
        start = len(isolated_store.events)
        _, result = rpc(client, "tools/call", {
            "name": "guild_paid_operations", "arguments": {},
        }, {**transport_headers, **headers}, 2)
        assert not result.get("isError"), result
        events = isolated_store.events[start:]
        assert len(events) == 8
        assert all(e["ua"] == "mcp:MCPClient/1.0.0" for e in events)
        assert all(e["surface"] == "mcp" for e in events)
        assert all(e["fp"] is bool(expected) for e in events)
        if expected:
            assert all(attribution.caller_class(e) == expected for e in events)
            assert not any(attribution.is_genuine_external(e) for e in events)
        else:
            assert all(attribution.is_genuine_external(e) for e in events)
        assert "test-current-token" not in json.dumps(events)
        assert "test-previous-token" not in json.dumps(events)

        # Authentication belongs to this call, not the MCP session. A later
        # call without its header must retain its ordinary external identity.
        start = len(isolated_store.events)
        rpc(client, "tools/call", {
            "name": "guild_paid_operations", "arguments": {},
        }, transport_headers, 3)
        later = isolated_store.events[start:]
        assert len(later) == 8 and all(e["fp"] is False for e in later)
        assert all(attribution.is_genuine_external(e) for e in later)


def test_context_isolated_across_concurrent_calls_and_reset_on_error(isolated_store):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def owned():
            with pytest.raises(RuntimeError):
                with firstparty.event_context("test-current-token", role="test"):
                    entered.set()
                    await release.wait()
                    isolated_store.record_event("owned", "query", ua="mcp:MCPClient/1.0.0",
                                                fp=False)
                    raise RuntimeError("synthetic tool failure")
            isolated_store.record_event("after", "query")

        async def external():
            await entered.wait()
            isolated_store.record_event("external", "query", ua="mcp:MCPClient/1.0.0")
            release.set()

        await asyncio.gather(owned(), external())
    asyncio.run(run())
    by_key = {e["key"]: e for e in isolated_store.events}
    assert by_key["owned"]["fp"] is True
    assert by_key["external"]["fp"] is False
    assert by_key["after"]["fp"] is False
    assert firstparty.event_metadata() == {}


@pytest.mark.parametrize("at,expected", [
    ("2026-09-08T13:16:45.477650+00:00", False),
    ("2026-09-08T13:16:45.477651+00:00", True),
    ("2026-09-08T13:16:46.276347+00:00", True),
    ("2026-09-08T13:16:46.276348+00:00", False),
])
def test_adk_incident_is_bounded_to_observed_validation_window(at, expected):
    event = {"key": "mcp:net:5846e2310912", "type": "paid_offer_served",
             "ua": "mcp:MCPClient/1.0.0", "fp": False, "at": at}
    assert attribution._is_known_first_party_incident(event) is expected
    assert attribution.is_genuine_external(event) is (not expected)
    assert event["fp"] is False  # historical source remains unchanged
    assert not attribution._is_known_first_party_incident({
        **event, "ua": "mcp:another-client/1.0.0"})
