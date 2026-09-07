"""Clients can execute discovery without inventing identity or funding steps.

All traffic is local conformance traffic; no wallet or outside adoption is involved.
"""
import asyncio
import base64
import json

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client

from app import payments, pricing, x402
from app.main import app
from app.mcp_server import mcp
from app.state import store


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    with TestClient(app) as value:
        yield value


@pytest.mark.parametrize("transport", ["http", "mcp"])
def test_manifest_recipe_funds_lookup_without_registration(client, transport):
    agents_before = set(store.agents)
    revenue_before = client.get("/billing/revenue").json()["real_settlement"]["revenue_usd"]
    manifest = client.get("/.well-known/agent-guild.json").json()
    first = manifest["first_use"]
    recipe = first["capability_trial"]
    acquire = recipe["acquire"]
    credential = acquire["credential"]
    assert first["registration_required"] is False
    assert acquire["registration_required"] is False
    assert acquire["request_body_required"] is False
    assert credential["secret"] is True

    trial = client.request(acquire["method"], acquire["path"])
    assert trial.status_code == 200
    account = trial.json()
    key = account[credential["response_field"]]
    balance = account[acquire["balance_response_field"]]
    request = recipe["request"]
    capability = "anonymous.first.use.conformance"
    if transport == "http":
        response = client.request(
            request["method"], request["path"],
            params={field: capability for field in request["query"]},
            headers={credential["http_header"]: key})
        assert response.status_code == 200
        result = response.json()
    else:
        async def lookup():
            async with Client(mcp) as connected:
                return await connected.call_tool(recipe["mcp"]["tool"], {
                    recipe["mcp"]["capability_argument"]: capability,
                    credential["mcp_argument"]: key,
                }, raise_on_error=False)
        response = asyncio.run(lookup())
        assert not response.is_error
        result = response.structured_content
    assert "verdict" in result
    assert result["shortlist"] == []  # service operation, not proof of a useful hire
    remaining = client.get("/billing/account", headers={credential["http_header"]: key})
    assert remaining.json()["balance"] == balance - request["cost_credits"]
    assert set(store.agents) == agents_before
    assert client.get("/billing/revenue").json()["real_settlement"]["revenue_usd"] == revenue_before


def _fixed_requests():
    return [
        payments.check_request("fact-check"),
        payments.check_request("fact-check", signed=True),
        payments.reputation_request("agt_example"),
        payments.evidence_request("agt_example"),
        payments.risk_score_request("agt_example"),
        payments.agent_flags_request("agt_example"),
        payments.payment_decision_request("a" * 64),
        payments.machine_envelope_request("a" * 64),
        payments.deep_preflight_request("https://example.com/mcp"),
        payments.evidence_bundle_request("https://example.com/mcp"),
        payments.watch_cycle_request("https://example.com/mcp"),
    ]


def test_manifest_funding_covers_gateway_products(client):
    manifest = client.get("/.well-known/agent-guild.json").json()
    expected = {request.operation: request.cost for request in _fixed_requests()}
    assert manifest["economics"]["pricing_credits"] == expected
    funding = manifest["payments"]["operation_funding"]
    assert set(funding["credit_funded"]) == set(expected)
    assert set(funding["x402_funded"]) == set(expected) | {"protected_payment_decision"}
    assert "protected_payment_decision" not in funding["credit_funded"]
    assert "watch_provision" not in expected
    assert manifest["economic_layer"]["real_money"] is False
    assert manifest["economic_layer"]["unit"] == "credits_sandbox"


@pytest.mark.parametrize("override", [0, 37, 123])
def test_discovery_and_charges_share_live_prices(client, monkeypatch, override):
    monkeypatch.delenv("GUILD_PRICE_BEST_AGENT", raising=False)
    monkeypatch.setattr(pricing, "_RUNTIME", {"best_agent": override})
    monkeypatch.setenv("GUILD_PRICE_PAYMENT_DECISION", "27")
    manifest = client.get("/.well-known/agent-guild.json").json()
    assert manifest["economics"]["pricing_credits"]["best_agent"] == override
    assert manifest["endpoints"]["check"]["cost_credits"] == override
    assert manifest["machine_payments"]["example_paid_resource"]["cost_credits"] == override
    assert manifest["first_use"]["capability_trial"]["request"]["cost_credits"] == override
    assert manifest["endpoints"]["payment_decision"]["cost_credits"] == 27
    assert payments.payment_decision_request("a" * 64).cost == 27
    assert "best_agent" in manifest["payments"]["operation_funding"]["x402_funded"]
    assert client.get("/").json()["pricing_credits"]["best_agent"] == override
    assert client.get("/.well-known/ai-plugin.json").json()["pricing"]["credits"]["best_agent"] == override
    account = client.post("/billing/trial").json()
    assert account["pricing"]["best_agent"] == override
    response = client.get("/check?capability=fact-check", headers={"X-API-Key": account["key"]})
    assert response.status_code == 200
    after = client.get("/billing/account", headers={"X-API-Key": account["key"]}).json()
    assert after["balance"] == account["balance"] - override
    if override:
        quote = client.get("/check?capability=fact-check")
        assert quote.status_code == 402
        challenge = json.loads(base64.b64decode(quote.headers["PAYMENT-REQUIRED"]))
        assert int(challenge["accepts"][0]["amount"]) == override * x402.ATOMIC_PER_CREDIT


def test_public_agent_guides_share_the_current_recipe(client):
    guide = client.get("/for-agents").text
    assert client.get("/agents.md").text == guide
    assert "POST /billing/trial" in guide and "X-API-Key: <key>" in guide
    assert "Real-money escrow is not" in guide
    for path in ("/auth.md", "/llms.txt"):
        text = client.get(path).text
        assert "X-API-Key: <key>" in text
        assert "api_key=<key>" in text
