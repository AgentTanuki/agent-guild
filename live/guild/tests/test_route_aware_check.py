"""A capability check must not invite hiring or attestations without a route.

Local fixtures and protocol probes are test evidence, never outside adoption.
"""
import asyncio
import copy
import json

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client

from app import a2a, a2a_x402, main, mcp_server, reachability as R
from app.bootstrap_eval import seed_bootstrap_evaluation
from app.store import Store


@pytest.fixture()
def market(monkeypatch):
    s = Store(path="")
    seed_bootstrap_evaluation(s)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "0")
    monkeypatch.setenv("GUILD_X402_ENABLED", "0")
    for module in (main, mcp_server, a2a, a2a_x402):
        monkeypatch.setattr(module, "store", s)
    # A /check is a pure evidence read, not permission for outbound probes.
    def unexpected_probe(*args, **kwargs):
        pytest.fail("capability lookup must not probe the network")
    monkeypatch.setattr(R, "liveness_probe", unexpected_probe)
    return s


def _top(market):
    return market.get_agent(market.shortlist("fact-check", limit=1)[0]["id"])


def _assert_no_route(result):
    assert result["presentation_version"] == "check-route-v1"
    assert result["routing"]["routable"] is False
    assert result["best_agent"]["recommended_for_routing"] is False
    if result["decision"] is not None:
        assert result["decision"]["recommended_for_routing"] is False
    if result["verdict"] is not None:
        assert result["verdict"]["recommendation"] != "hire"
        assert result["verdict"]["actionable"] is False
    next_call = result["guild_next"]
    assert next_call["delegation_ready"] is False
    assert "POST /collaborations" not in next_call["one_call"]
    assert "or_attest" not in next_call
    assert next_call["buyer_action"]["path"] != next_call["supplier_action"]["path"]


@pytest.mark.parametrize("state", [
    "no_endpoint", "declared_unverified", "currently_unreachable",
    "http_responsive", "expired", "endpoint_changed",
])
def test_unroutable_evidence_does_not_emit_hire_or_task_write(market, state):
    agent = _top(market)
    endpoint = "https://worker.example/a2a"
    if state != "no_endpoint":
        agent["metadata"]["endpoint"] = endpoint
    if state not in ("no_endpoint", "declared_unverified"):
        status = state if state in ("currently_unreachable", "http_responsive") else "recently_reachable"
        record = R.make_record(status, "test_probe", "protocol_handshake", endpoint)
        if state == "expired":
            record["expires_at"] = "2000-01-01T00:00:00+00:00"
        if state == "endpoint_changed":
            agent["metadata"]["endpoint"] = "https://changed.example/a2a"
        agent["reachability"] = record
    evidence = copy.deepcopy(market.risk_for(agent["id"]))
    assert evidence["recommendation"] == "hire"  # reproduce the live contradiction
    result = market.check("fact-check")
    _assert_no_route(result)
    assert result["verdict"]["actionability_reason"] == "no_verified_route"
    assert result["verdict"]["counterparty_first_party"] is True
    assert market.risk_for(agent["id"]) == evidence
    for field in ("estimate", "confidence", "risk", "staleness", "explanation"):
        assert result["verdict"][field] == evidence[field]
    action = result["guild_next"]["buyer_action"]
    if state == "no_endpoint":
        assert action["path"] == "/demand/watch"
        assert action["credential_kind"] == "registered_agent_X-API-Key"
    else:
        assert action["path"] == f"/agents/{agent['id']}/endpoint/refresh"
        assert action["body_allowed"] is False
        assert action["credential_required"] is False


@pytest.mark.parametrize("recommendation", ["caution", "avoid"])
def test_route_guard_does_not_promote_an_existing_warning(market, monkeypatch, recommendation):
    original = market.risk_for
    monkeypatch.setattr(market, "risk_for", lambda ident: {
        **original(ident), "recommendation": recommendation})
    result = market.check("fact-check")
    _assert_no_route(result)
    assert result["verdict"]["recommendation"] == recommendation


def test_verified_lower_ranked_route_is_the_evaluated_counterparty(market):
    top = _top(market)
    endpoint = "https://lower.example/a2a"
    lower = market.register_agent("Reachable lower", ["fact-check"], {"endpoint": endpoint})
    lower["reachability"] = R.invocation_verified_record(endpoint, "test-invocation")
    result = market.check("fact-check")
    assert result["highest_ranked"]["agent_id"] == top["id"]
    assert result["routing"]["routable"] is True
    assert result["best_agent"]["id"] == result["decision"]["agent_id"] == lower["id"]
    assert result["verdict"]["agent_id"] == result["routing"]["provider_id"] == lower["id"]
    assert result["verdict"]["recommendation"] == market.risk_for(lower["id"])["recommendation"]
    assert "actionable" not in result["verdict"]  # caller still owns approval policy
    assert "POST /collaborations" in result["guild_next"]["one_call"]
    assert "Only after" in result["guild_next"]["high_value_action"]


def test_binding_failure_clears_all_selected_action_flags(market, monkeypatch):
    agent = _top(market)
    endpoint = "https://worker.example/a2a"
    agent["metadata"]["endpoint"] = endpoint
    agent["reachability"] = R.invocation_verified_record(endpoint, "test-invocation")
    original = market.get_agent
    reads = 0
    def conflicting_identity(ident):
        nonlocal reads
        record = original(ident)
        if ident == agent["id"]:
            reads += 1
            if reads > 1:
                return {**record, "did": "did:key:different-counterparty"}
        return record
    monkeypatch.setattr(market, "get_agent", conflicting_identity)
    result = market.check("fact-check")
    _assert_no_route(result)
    assert result["routing"]["reason_code"] == "counterparty_binding_failed"
    assert result["verdict"]["actionability_reason"] == "counterparty_binding_failed"
    assert result["verdict"]["counterparty_first_party"] is None
    assert result["guild_next"]["buyer_action"]["path"] == "/health"


def test_guided_public_refresh_can_restore_a_route_without_owner_key(market, monkeypatch):
    endpoint = "https://new.example/a2a"
    new = market.register_agent("New declared supplier", ["fact-check"], {"endpoint": endpoint})
    # Persist fixture metadata so the public refresh's authoritative read sees it.
    market._save()
    initial = market.check("fact-check")
    action = initial["guild_next"]["buyer_action"]
    assert action["probe_target_id"] == new["id"]
    assert initial["decision"]["agent_id"] != new["id"]
    monkeypatch.setattr(R, "liveness_probe", lambda url: R.make_record(
        "recently_reachable", "protocol_probe", "protocol_handshake", url))
    with TestClient(main.app) as client:
        refreshed = client.request(action["method"], action["path"])
        forbidden_body = client.request(action["method"], action["path"], json={})
    assert refreshed.status_code == 200
    assert forbidden_body.status_code == 422
    assert refreshed.json()["recommended_for_routing"] is True
    after = market.check("fact-check")
    assert after["routing"]["routable"] is True
    assert after["decision"]["agent_id"] == after["routing"]["provider_id"] == new["id"]
    assert after["decision"]["confidence"] == 0  # protocol checks create no reputation


def test_http_mcp_a2a_present_the_same_blocked_verdict(market):
    async def mcp_check():
        async with Client(mcp_server.mcp) as client:
            return (await client.call_tool("guild_check", {"capability": "fact-check"})).structured_content
    with TestClient(main.app) as client:
        http_result = client.get("/check?capability=fact-check").json()
        mcp_result = asyncio.run(mcp_check())
        response = client.post("/a2a", json={
            "jsonrpc": "2.0", "id": "route-conformance", "method": "message/send",
            "params": {"message": {"role": "user", "parts": [
                {"kind": "text", "text": "check: fact-check"}]}}})
        a2a_result = json.loads(response.json()["result"]["parts"][0]["text"])
    for result in (http_result, mcp_result, a2a_result):
        _assert_no_route(result)
        assert result["verdict"] == http_result["verdict"]
        assert result["guild_next"] == http_result["guild_next"]


def test_signed_decision_carries_version_and_blocked_route(market):
    result = market.signed_decision("fact-check")
    assert result["presentation_version"] == "check-route-v1"
    assert result["routing"]["routable"] is False
    assert result["decision"]["recommended_for_routing"] is False
    assert result["decision"]["estimate"] == market.risk_for(
        result["decision"]["agent_id"])["estimate"]
