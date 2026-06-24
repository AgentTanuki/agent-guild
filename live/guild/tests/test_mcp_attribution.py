"""MCP client attribution: genuine third-party MCP usage must be distinguishable
from our own tests in the adoption funnel.

Previously every MCP tool recorded a hardcoded ``ua="mcp/remote"`` — so a real
external agent arriving over MCP (the channel distribution drives) was invisible.
The server now records the connecting client's ``clientInfo`` from the initialize
handshake as ``mcp:<name>/<version>``, and the external-agent detector counts any
non-baseline mcp client as genuine external.
"""
import asyncio
import os
import sys

os.environ.setdefault("GUILD_DATA", "")

import mcp.types as mt  # noqa: E402
from fastmcp import Client  # noqa: E402

from app.mcp_server import mcp as guild_mcp, _client_ua  # noqa: E402
from app.state import store  # noqa: E402

# import the scheduled detector (live/scripts) for its classification helpers
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import detect_external as det  # noqa: E402


def _search_as(client_info):
    async def run():
        async with Client(guild_mcp, client_info=client_info) as c:
            await c.call_tool("guild_search", {"capability": "fact-check"})
    asyncio.run(run())


def test_mcp_call_records_connecting_client_identity():
    _search_as(mt.Implementation(name="cursor", version="1.2"))
    last = store.events[-1]
    assert last["type"] == "query"
    assert last["ua"] == "mcp:cursor/1.2"  # not the old hardcoded "mcp/remote"


def test_client_ua_falls_back_safely_without_client_info():
    # never raise, even if no MCP context is available
    assert _client_ua(None) == "mcp/remote"


def test_detector_parses_mcp_client_name():
    assert det._mcp_client("mcp:langchain-mcp-adapter/9.9") == "langchain-mcp-adapter"
    assert det._mcp_client("mcp:Cursor/1.0") == "cursor"
    assert det._mcp_client("mcp/remote") is None      # legacy, unattributable
    assert det._mcp_client("python-httpx/0.27") is None


def test_detector_treats_nonbaseline_mcp_client_as_external():
    assert "cursor" not in det.OURS_MCP_CLIENTS        # a real third-party client
    assert "verify" in det.OURS_MCP_CLIENTS            # our own smoke test, excluded
