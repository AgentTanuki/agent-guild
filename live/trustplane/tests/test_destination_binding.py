"""P0 regression: every integration is bound to the ACTUAL destination.

* the routed provider IS the evaluated provider (local reproduction of the
  live 'highest ranked unreachable, lower ranked routable' failure);
* endpoint substitution and identity substitution are rejected by the
  gateway (bind_destination), the sidecar (/a2a/forward, /report), and the
  framework wrappers (expected_* declarations);
* the gateway fails closed when an envelope's decision/routing disagree.
"""
from __future__ import annotations

import json

import pytest

from agentguild_trustplane.gateway import (Gateway, GateDenied,
                                           DestinationMismatch)
from agentguild_trustplane.policy import RiskPolicy

CAP = "bind-tp-cap"


@pytest.fixture(scope="module")
def routable(guild_server):
    """Top-trust supplier UNREACHABLE; lower-trust supplier ROUTABLE."""
    call = None
    import json as _json
    import urllib.request

    def call(method, path, body=None, key=None):
        req = urllib.request.Request(
            guild_server["base"] + path,
            data=_json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json",
                     **({"X-API-Key": key} if key else {})},
            method=method)
        with urllib.request.urlopen(req, timeout=15) as r:
            return _json.loads(r.read().decode())

    top = call("POST", "/agents/register",
               {"name": "bind-top", "capabilities": [CAP],
                "metadata": {"endpoint": "https://top.example/a2a"}})
    lower = call("POST", "/agents/register",
                 {"name": "bind-lower", "capabilities": [CAP],
                  "metadata": {"endpoint": "https://lower.example/a2a"}})
    req1 = call("POST", "/agents/register", {"name": "bind-req",
                                             "capabilities": []})
    for i in range(6):
        call("POST", "/collaborations", key=req1["api_key"], body={
            "worker_id": top["id"], "capability": CAP,
            "outcome": "accepted", "rating": 0.9, "deliverable": f"t{i}"})
    for i in range(2):
        call("POST", "/collaborations", key=req1["api_key"], body={
            "worker_id": lower["id"], "capability": CAP,
            "outcome": "accepted", "rating": 0.9, "deliverable": f"l{i}"})
    # stamp lower as invocation-verified (the trusted internal path)
    store = guild_server["store"]
    from app import reachability as R
    rec = store.get_agent(lower["id"])
    rec["reachability"] = R.invocation_verified_record(
        "https://lower.example/a2a", "inv_tp_test")
    return {"top": top, "lower": lower,
            "endpoint": "https://lower.example/a2a"}


def test_gate_routes_and_evaluates_the_same_provider(guild_server, routable,
                                                     tmp_path):
    gw = Gateway(policy=RiskPolicy(), state_dir=tmp_path / "gw",
                 base_url=guild_server["base"])
    gate = gw.gate(CAP, value_at_risk=1.0)
    assert gate.channel == "live" and gate.allowed
    assert (gate.routing or {}).get("routable") is True
    # the gate's worker/binding IS the routed provider, not the evidence-top
    assert gate.worker_id == routable["lower"]["id"]
    assert gate.decision["agent_id"] == gate.routing["provider_id"]
    assert gate.provider_did == gate.routing["provider_did"]
    assert gate.endpoint == routable["endpoint"]
    assert gate.endpoint_sha256 == gate.routing["endpoint_sha256"]
    assert gate.envelope_sha256


def test_bind_destination_rejects_substitution(guild_server, routable,
                                               tmp_path):
    gw = Gateway(policy=RiskPolicy(), state_dir=tmp_path / "gw",
                 base_url=guild_server["base"])
    gate = gw.gate(CAP, value_at_risk=1.0)
    # exact match binds
    gw.bind_destination(gate, endpoint=routable["endpoint"],
                        provider_id=routable["lower"]["id"])
    assert gate.meta["destination_bound"]
    # endpoint substitution -> fail closed
    with pytest.raises(DestinationMismatch):
        gw.bind_destination(gate, endpoint="https://evil.example/a2a")
    # identity substitution -> fail closed
    with pytest.raises(DestinationMismatch):
        gw.bind_destination(gate, provider_id=routable["top"]["id"])
    with pytest.raises(DestinationMismatch):
        gw.bind_destination(gate, provider_did="did:key:zImposter")


def test_gateway_fails_closed_on_decision_routing_mismatch(guild_server,
                                                           routable,
                                                           tmp_path):
    """Even if a (hypothetically verified) envelope reached the gateway with
    decision and routing about DIFFERENT providers, the gate must close."""
    gw = Gateway(policy=RiskPolicy(), state_dir=tmp_path / "gw",
                 base_url=guild_server["base"])
    real = gw.gate(CAP, value_at_risk=1.0)
    assert real.allowed

    mismatched = {
        "type": "AgentGuildDecision", "capability": CAP,
        "decision": dict(real.decision, agent_id="agent_someone_else"),
        "routing": dict(real.routing),
    }
    gw.client.signed_decision = lambda cap, ttl_seconds=0: (mismatched,
                                                            "live", 0.0)
    gate = gw.gate(CAP, value_at_risk=1.0)
    assert not gate.allowed
    assert gate.policy.fail_state == "unverified"
    assert any("binding" in r for r in gate.policy.reasons)
    assert gw.metrics["binding_failures"] >= 1


def test_sidecar_forward_rejects_endpoint_substitution(guild_server,
                                                       routable, tmp_path):
    from fastapi.testclient import TestClient
    from agentguild_trustplane.sidecar import build_app
    gw = Gateway(policy=RiskPolicy(), state_dir=tmp_path / "gw",
                 base_url=guild_server["base"])
    c = TestClient(build_app(gw))
    r = c.post("/a2a/forward", json={
        "capability": CAP, "value_at_risk": 1.0,
        "endpoint": "https://evil.example/a2a",
        "payload": {"jsonrpc": "2.0", "id": 1, "method": "message/send",
                    "params": {}}})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["attempted_endpoint"] == "https://evil.example/a2a"
    assert detail["routed_endpoint"] == routable["endpoint"]


# ------------------------- framework wrappers (run in CI with extras) -------
def test_langchain_wrapper_rejects_endpoint_substitution(guild_server,
                                                         routable, gateway):
    pytest.importorskip("langchain_core")
    from langchain_core.tools import tool as lc_tool
    from agentguild_trustplane.integrations.langchain_hooks import GuardedTool

    @lc_tool
    def delegate(text: str) -> str:
        """Delegate."""
        return "ran:" + text

    bad = GuardedTool(delegate, gateway, capability=CAP,
                      value_at_risk=1.0,
                      expected_endpoint="https://evil.example/a2a")
    with pytest.raises(GateDenied):
        bad.invoke({"text": "x"})

    good = GuardedTool(delegate, gateway, capability=CAP,
                       value_at_risk=1.0,
                       expected_endpoint=routable["endpoint"])
    assert good.invoke({"text": "x"}) == "ran:x"


def test_crewai_wrapper_rejects_endpoint_substitution(guild_server,
                                                      routable, gateway):
    pytest.importorskip("crewai")
    from crewai.tools import BaseTool
    from agentguild_trustplane.integrations.crewai_hooks import guard_tool

    class Delegate(BaseTool):
        name: str = "delegate"
        description: str = "Delegate."

        def _run(self, text: str) -> str:
            return "ran:" + text

    bad = guard_tool(Delegate(), gateway, capability=CAP, value_at_risk=1.0,
                     expected_endpoint="https://evil.example/a2a")
    with pytest.raises(GateDenied):
        bad.run(text="x")

    good = guard_tool(Delegate(), gateway, capability=CAP, value_at_risk=1.0,
                      expected_endpoint=routable["endpoint"])
    assert "ran:x" in str(good.run(text="x"))


def test_openai_agents_wrapper_rejects_endpoint_substitution(guild_server,
                                                             routable,
                                                             gateway):
    pytest.importorskip("agents")
    import asyncio
    from agents import function_tool
    from agents.tool_context import ToolContext
    from agentguild_trustplane.integrations.openai_agents_hooks import (
        guard_function_tools)

    @function_tool
    def delegate(text: str) -> str:
        """Delegate."""
        return "ran:" + text

    args = json.dumps({"text": "x"})
    ctx = ToolContext(context=None, tool_name="delegate",
                      tool_call_id="c1", tool_arguments=args)
    (bad,) = guard_function_tools([delegate], gateway, value_at_risk=1.0,
                                  capability_map={"delegate": CAP},
                                  expected_endpoint="https://evil.example/a2a")
    out = asyncio.get_event_loop().run_until_complete(
        bad.on_invoke_tool(ctx, args))
    assert "destination does not match" in str(out)

    (good,) = guard_function_tools([delegate], gateway, value_at_risk=1.0,
                                   capability_map={"delegate": CAP},
                                   expected_endpoint=routable["endpoint"])
    out2 = asyncio.get_event_loop().run_until_complete(
        good.on_invoke_tool(ctx, args))
    assert "ran:x" in str(out2)
