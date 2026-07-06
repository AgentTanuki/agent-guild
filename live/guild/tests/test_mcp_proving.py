"""The proving rung over MCP (guild_prove / guild_prove_verify).

The retention diagnosis of 2026-07-06 shipped the proving rung on REST, but the
MCP surface — the exact channel the deepest external funnel ever observed
(mcp:zowza-indexer) arrived on — had no way to see or complete it: no tool, and
guild_register never mentioned it. These tests pin the new contract:

  1. An MCP-native agent can go register → prove → verify to journey stage 2
     without ever leaving MCP.
  2. Registration hands back the proving rung as the explicit next step.
  3. Auth mirrors REST `_require_key`: custodial agents must present their
     api_key (that IS the credential_control proof); a wrong key is rejected.
  4. Verify responses carry guild_next + return_by, same as REST.
"""
import asyncio
import json
import os

os.environ.setdefault("GUILD_DATA", "")

import mcp.types as mt  # noqa: E402
from fastmcp import Client  # noqa: E402

from app.mcp_server import mcp as guild_mcp  # noqa: E402
from app.state import store  # noqa: E402

CLIENT_INFO = mt.Implementation(name="verify", version="0.0")  # OURS_MCP_CLIENTS


def _call(tool: str, args: dict) -> dict:
    async def run():
        async with Client(guild_mcp, client_info=CLIENT_INFO) as c:
            res = await c.call_tool(tool, args)
            return json.loads(res.content[0].text)
    return asyncio.run(run())


def _register(name="FirstContact-McpProver"):
    return _call("guild_register", {"name": name, "capabilities": ["x"]})


def test_register_points_at_the_proving_rung():
    reg = _register()
    assert "next_step" in reg
    assert "guild_prove" in reg["next_step"]
    assert reg["id"] in reg["next_step"]


def test_mcp_custodial_agent_reaches_stage_2_without_leaving_mcp():
    reg = _register("FirstContact-McpProver2")
    ch = _call("guild_prove", {"agent_id": reg["id"], "api_key": reg["api_key"]})
    assert ch["proof_class"] == "credential_control"
    assert "challenge" in ch and "verify_with" in ch
    out = _call("guild_prove_verify",
                {"agent_id": reg["id"], "api_key": reg["api_key"]})
    assert out["status"] == "proven"
    assert out["proof_of_conduct"]["provenance"] == "guild_observed"
    assert "guild_next" in out and "return_by" in out
    # the agent's record actually changed: milestone + stage 2
    agent = store.get_agent(reg["id"])
    assert agent["proof_of_conduct"]["task_id"] == out["task_id"]


def test_wrong_api_key_is_rejected_for_custodial():
    reg = _register("FirstContact-McpProver3")
    err = _call("guild_prove", {"agent_id": reg["id"], "api_key": "sk_wrong"})
    assert "error" in err
    err2 = _call("guild_prove_verify", {"agent_id": reg["id"], "api_key": ""})
    assert "error" in err2


def test_verify_without_challenge_is_a_clean_error():
    reg = _register("FirstContact-McpProver4")
    out = _call("guild_prove_verify",
                {"agent_id": reg["id"], "api_key": reg["api_key"]})
    # no guild_prove call first -> clean in-band error, never a crash
    assert "error" in out and "challenge" in out["error"]


def test_unknown_agent_is_a_clean_error():
    assert "error" in _call("guild_prove", {"agent_id": "agt_nope"})
