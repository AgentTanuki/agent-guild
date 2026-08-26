"""Public MCP server-card discovery stays truthful and non-attributing."""
from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient
from mcp.types import LATEST_PROTOCOL_VERSION

from app import __version__
from app.main import app
from app.mcp_server import (
    mcp,
    payment_safety_mcp,
    public_payment_safety_server_card,
    public_server_card,
    public_trust_read_server_card,
    trust_read_mcp,
)
from app.state import store


client = TestClient(app)


def _paid_offer_events():
    return [
        event for event in store.events
        if event.get("type") in {
            "offer_served", "paid_offer_served", "paid_offer_shown"
        }
    ]


def test_server_card_matches_live_tool_registry_without_demand_events():
    before = list(_paid_offer_events())
    response = client.get(
        "/.well-known/mcp/server-card.json",
        headers={"Origin": "https://crawler.example"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["cache-control"] == (
        "public, max-age=300, s-maxage=300"
    )
    card = response.json()

    expected_tools = [
        tool.to_mcp_tool().model_dump(by_alias=True, exclude_none=True)
        for tool in asyncio.run(mcp.list_tools())
    ]
    assert card["tools"] == expected_tools
    assert "/.well-known/mcp/server-card.json" in (
        client.get("/openapi.json").json()["paths"]
    )
    assert _paid_offer_events() == before


def test_short_well_known_alias_matches_server_card():
    """Open MCP crawlers commonly probe ``/.well-known/mcp.json``."""
    before = list(_paid_offer_events())
    alias = client.get(
        "/.well-known/mcp.json",
        headers={"Origin": "https://crawler.example"},
    )
    canonical = client.get("/.well-known/mcp/server-card.json")

    assert alias.status_code == 200
    assert alias.headers["content-type"].startswith("application/json")
    assert alias.headers["access-control-allow-origin"] == "*"
    assert alias.headers["cache-control"] == (
        "public, max-age=300, s-maxage=300"
    )
    assert alias.json() == canonical.json()
    assert "/.well-known/mcp.json" in (
        client.get("/openapi.json").json()["paths"]
    )
    assert _paid_offer_events() == before


def test_server_card_advertises_only_truthful_public_transport_metadata():
    card = asyncio.run(public_server_card())
    assert "$schema" not in card  # the SEP-1649 draft URL is not live
    assert "version" not in card  # avoid ambiguity with serverInfo.version
    assert card["protocolVersion"] == LATEST_PROTOCOL_VERSION
    assert card["serverInfo"] == {
        "name": "Agent Guild",
        "title": "Agent Guild",
        "version": __version__,
    }
    assert card["transport"] == {
        "type": "streamable-http",
        "endpoint": "/mcp/",
    }
    assert card["authentication"] == {"required": False, "schemes": []}
    assert card["resources"] == [
        resource.to_mcp_resource().model_dump(by_alias=True, exclude_none=True)
        for resource in asyncio.run(mcp.list_resources())
    ]
    assert card["resourceTemplates"] == [
        template.to_mcp_template().model_dump(by_alias=True, exclude_none=True)
        for template in asyncio.run(mcp.list_resource_templates())
    ]
    assert card["prompts"] == [
        prompt.to_mcp_prompt().model_dump(by_alias=True, exclude_none=True)
        for prompt in asyncio.run(mcp.list_prompts())
    ]


def test_server_card_exposes_payment_fallback_only_on_paid_reads():
    card = asyncio.run(public_server_card())
    tools = {tool["name"]: tool for tool in card["tools"]}
    for name in ("guild_check", "guild_search", "guild_best_agent",
                 "guild_risk_score", "guild_preflight_deep",
                 "guild_x402_payment_safety"):
        schema = tools[name]["inputSchema"]["properties"]["x402_payment"]
        serialized = json.dumps(schema)
        assert '"object"' in serialized
        assert '"string"' in serialized
    for name in ("guild_register", "guild_attest"):
        assert "x402_payment" not in (
            tools[name]["inputSchema"].get("properties") or {}
        )


def test_server_card_names_the_pre_signature_payment_tool_truthfully():
    card = asyncio.run(public_server_card())
    tool = {item["name"]: item for item in card["tools"]}[
        "guild_x402_payment_safety"]
    assert tool["title"] == "Authorize an x402 payment before signing"
    assert tool["description"].startswith(
        "PAID. Call this immediately BEFORE signing an x402 payment envelope.")
    props = tool["inputSchema"]["properties"]
    assert set(props["payment"]["required"]) == {
        "scheme", "network", "asset", "amount", "pay_to", "resource"
    }
    assert tool["_meta"]["ai.agent-guild/paid"]["operation"] == \
        "payment_decision"


def test_focused_payment_safety_card_matches_its_exact_one_tool_registry():
    before = list(_paid_offer_events())
    response = client.get(
        "/.well-known/mcp/payment-safety-server-card.json",
        headers={"Origin": "https://crawler.example"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    card = response.json()
    expected = [
        tool.to_mcp_tool().model_dump(by_alias=True, exclude_none=True)
        for tool in asyncio.run(payment_safety_mcp.list_tools())
    ]
    assert card["tools"] == expected
    assert [tool["name"] for tool in card["tools"]] == [
        "guild_x402_payment_safety"]
    assert card["transport"]["endpoint"] == "/mcp/payment-safety/"
    assert card["serverInfo"]["name"] == \
        "Agent Guild x402 Payment Safety"
    assert asyncio.run(public_payment_safety_server_card()) == card
    assert _paid_offer_events() == before


def test_focused_trust_card_matches_its_exact_least_authority_registry():
    before = list(_paid_offer_events())
    response = client.get(
        "/.well-known/mcp/trust-server-card.json",
        headers={"Origin": "https://crawler.example"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["cache-control"] == (
        "public, max-age=300, s-maxage=300"
    )
    card = response.json()
    expected = [
        tool.to_mcp_tool().model_dump(by_alias=True, exclude_none=True)
        for tool in asyncio.run(trust_read_mcp.list_tools())
    ]
    assert card["tools"] == expected
    assert sorted(tool["name"] for tool in card["tools"]) == [
        "guild_best_agent",
        "guild_check",
        "guild_index",
        "guild_passport",
        "guild_preflight",
        "guild_preflight_deep",
        "guild_risk_score",
        "guild_search",
        "guild_verify",
    ]
    assert card["transport"]["endpoint"] == "/mcp/trust/"
    assert card["serverInfo"]["name"] == "Agent Guild Trust Reads"
    assert asyncio.run(public_trust_read_server_card()) == card
    assert card["resources"] == []
    assert card["resourceTemplates"] == []
    assert card["prompts"] == []
    disallowed_inputs = {
        "api_key", "x402_payment", "token", "secret", "password",
        "private_key", "wallet_key", "authorization",
    }
    for tool in card["tools"]:
        properties = set(tool["inputSchema"].get("properties") or {})
        assert properties.isdisjoint(disallowed_inputs), (tool["name"], properties)
    verify = {tool["name"]: tool for tool in card["tools"]}["guild_verify"]
    assert set(verify["inputSchema"]["properties"]) == {"passport"}
    serialized = json.dumps(card["tools"]).lower()
    assert "api_key" not in serialized
    assert "x402_payment" not in serialized
    assert _paid_offer_events() == before
