"""LIVE, read-only smoke checks for the scout's registry adapters.

Fixture-only tests once concealed a dead A2A URL and two wrong protocol
assumptions. These tests hit the REAL registries — official MCP Registry,
a2aregistry.org, CDP Bazaar — through the scout's own SSRF-safe bounded
fetch, and assert the exact response-shape assumptions the adapters encode.

Read-only: list/search endpoints only; nothing is registered, invoked,
contacted or paid. Gated behind GUILD_LIVE_SMOKE=1 so offline CI stays
deterministic; the acceptance pipeline runs them explicitly.
"""
import json
import os

import pytest

from app.swarm import scout

pytestmark = pytest.mark.skipif(
    os.environ.get("GUILD_LIVE_SMOKE") != "1",
    reason="live network smoke checks run only with GUILD_LIVE_SMOKE=1")


def test_live_a2a_registry_shape_and_adapter_path():
    doc, reason = scout.safe_fetch_json("https://a2aregistry.org/api/agents",
                                        max_bytes=scout.MAX_REGISTRY_BYTES)
    assert reason == "ok", f"live A2A registry fetch failed: {reason}"
    assert isinstance(doc, dict) and isinstance(doc.get("agents"), list), (
        "a2aregistry.org/api/agents must serve {'agents': [...]}")
    assert doc["agents"], "live registry unexpectedly empty"
    a = doc["agents"][0]
    assert "name" in a and ("url" in a or "endpoint" in a)


def test_live_mcp_registry_search_shape():
    url = ("https://registry.modelcontextprotocol.io/v0.1/servers"
           "?search=legal&limit=5")
    doc, reason = scout.safe_fetch_json(url,
                                        max_bytes=scout.MAX_REGISTRY_BYTES)
    assert reason == "ok", f"live MCP Registry fetch failed: {reason}"
    servers = (doc or {}).get("servers")
    assert isinstance(servers, list) and servers, (
        "official MCP Registry must serve {'servers': [...]}")
    row = servers[0]
    srv = row.get("server") if isinstance(row.get("server"), dict) else row
    assert srv.get("name")


def test_live_bazaar_discovery_shape():
    url = ("https://api.cdp.coinbase.com/platform/v2/x402"
           "/discovery/resources?limit=5")
    doc, reason = scout.safe_fetch_json(url,
                                        max_bytes=scout.MAX_REGISTRY_BYTES)
    assert reason == "ok", f"live Bazaar fetch failed: {reason}"
    items = (doc or {}).get("items")
    assert isinstance(items, list) and items, (
        "CDP Bazaar must serve {'items': [...]}")
    it = items[0]
    assert it.get("resource", "").startswith("http")
    assert isinstance(it.get("accepts"), list) and it["accepts"], (
        "every Bazaar item carries x402 `accepts` payment requirements")


def test_live_adapters_yield_at_least_one_valid_candidate_path():
    """Where public supply exists for a broad term, at least one adapter must
    produce a policy-clean candidate — proof the URL + parse + qualify path
    works end to end against production registries."""
    found = []
    for name, adapter in scout.ADAPTERS.items():
        if name == "erc8004":
            continue                     # honestly unsupported
        try:
            found += adapter("search", scout.safe_fetch_json)
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"live adapter {name} crashed: {e}")
    assert found, "no adapter produced a candidate for a broad term"
    from app import reachability
    ok_policy = [c for c in found if c.get("endpoint")
                 and reachability.url_policy_check(c["endpoint"])[0]]
    assert ok_policy, (
        "no candidate passed URL policy — parse or shape assumptions broken: "
        + json.dumps(found[:3])[:500])
