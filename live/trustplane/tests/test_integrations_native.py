"""Framework interceptors exercised through each framework's NATIVE lifecycle.

No LLM keys exist in this environment, so what runs here is each framework's
real tool-execution machinery (the exact code path a model-planned run takes
when it reaches the tool), driven deterministically:

  crewai         BaseTool.run through the pydantic tool interface + the real
                 crewai_event_bus emitting ToolUsageFinishedEvent
  langchain      BaseTool.invoke (full callback pipeline)
  langgraph      prebuilt ToolNode invoked with a genuine tool_calls message
  openai-agents  FunctionTool.on_invoke_tool — the SDK's single execution
                 choke point, invoked exactly as Runner does
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agentguild_trustplane.gateway import GateDenied
from agentguild_trustplane.integrations.pins import check_pins


def _skip_unless(*mods):
    for m in mods:
        pytest.importorskip(m)


def test_pins_installed_versions_supported():
    pins = check_pins()
    for fw in ("crewai", "langchain-core", "langgraph", "openai-agents", "mcp"):
        if pins[fw]["installed"] is None:
            pytest.skip(f"{fw} not installed in this environment")
        assert pins[fw]["supported"], f"{fw}: {pins[fw]}"


# --------------------------------------------------------------------- crewai
def test_crewai_guard_blocks_and_allows(guild_server, seeded, gateway):
    _skip_unless("crewai")
    from crewai.tools import BaseTool

    class EchoTool(BaseTool):
        name: str = "tp-echo"
        description: str = "echo"

        def _run(self, text: str) -> str:
            return "echo:" + text

    from agentguild_trustplane.integrations.crewai_hooks import (
        guard_tool, TrustPlaneListener)

    tool = guard_tool(EchoTool(), gateway, value_at_risk=1.0)
    TrustPlaneListener(gateway, [tool])
    out = tool.run(text="hi")
    assert out == "echo:hi"
    assert gateway.snapshot()["gates"] >= 1

    denied = guard_tool(EchoTool(), gateway, capability="tp-echo",
                        value_at_risk=5000.0)      # high tier: thin evidence
    with pytest.raises(GateDenied):
        denied.run(text="hi")


def test_crewai_event_bus_listener_records(guild_server, seeded, gateway):
    _skip_unless("crewai")
    from crewai.events import crewai_event_bus
    from crewai.events.types.tool_usage_events import ToolUsageFinishedEvent
    from crewai.tools import BaseTool
    from agentguild_trustplane.integrations.crewai_hooks import (
        guard_tool, TrustPlaneListener)

    class T(BaseTool):
        name: str = "tp-echo"
        description: str = "d"

        def _run(self) -> str:
            return "ok"

    t = guard_tool(T(), gateway, value_at_risk=1.0)
    TrustPlaneListener(gateway, [t])
    before = gateway.outcomes.stats["recorded"]
    t.run()
    fut = crewai_event_bus.emit(t, ToolUsageFinishedEvent(
        tool_name="tp-echo", tool_args={}, output="ok",
        started_at=__import__("datetime").datetime.now(),
        finished_at=__import__("datetime").datetime.now(),
        agent_key="k", agent_role="r"))
    if fut is not None:                    # handlers run async on the bus
        fut.result(timeout=10)
    else:
        import time
        time.sleep(0.5)
    assert gateway.outcomes.stats["recorded"] > before


# ------------------------------------------------------------ langchain/graph
def test_langchain_guarded_tool_native_invoke(guild_server, seeded, gateway):
    _skip_unless("langchain_core")
    from langchain_core.tools import tool as lc_tool
    from agentguild_trustplane.integrations.langchain_hooks import GuardedTool

    @lc_tool
    def tp_echo(text: str) -> str:
        """Echo text back."""
        return "echo:" + text

    g = GuardedTool(tp_echo, gateway, capability="tp-echo", value_at_risk=1.0)
    assert g.invoke({"text": "hi"}) == "echo:hi"

    g_high = GuardedTool(tp_echo, gateway, capability="tp-echo",
                         value_at_risk=5000.0)
    with pytest.raises(GateDenied):
        g_high.invoke({"text": "hi"})


def test_langgraph_toolnode_runs_guarded_tools(guild_server, seeded, gateway):
    _skip_unless("langchain_core", "langgraph")
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool as lc_tool
    from langgraph.prebuilt import ToolNode
    from agentguild_trustplane.integrations.langchain_hooks import guard_tools

    @lc_tool
    def tp_echo(text: str) -> str:
        """Echo text back."""
        return "echo:" + text

    from langgraph.graph import StateGraph, MessagesState, START, END
    node = ToolNode(guard_tools([tp_echo], gateway, value_at_risk=1.0,
                                capability_map={"tp_echo": "tp-echo"}))
    g = StateGraph(MessagesState)
    g.add_node("tools", node)
    g.add_edge(START, "tools")
    g.add_edge("tools", END)
    compiled = g.compile()
    msg = AIMessage(content="", tool_calls=[{
        "name": "tp_echo", "args": {"text": "hi"}, "id": "call_1"}])
    res = compiled.invoke({"messages": [msg]})
    assert "echo:hi" in str(res)


# ------------------------------------------------------------- openai-agents
def test_openai_agents_function_tool_gated(guild_server, seeded, gateway):
    _skip_unless("agents")
    from agents import function_tool
    from agentguild_trustplane.integrations.openai_agents_hooks import (
        guard_function_tools)

    @function_tool
    def tp_echo(text: str) -> str:
        """Echo text back."""
        return "echo:" + text

    from agents.tool_context import ToolContext
    from agents.run_context import RunContextWrapper

    def ctx(args: str) -> ToolContext:
        return ToolContext(context=None, tool_name="tp_echo",
                           tool_call_id="call_1", tool_arguments=args)

    cm = {"tp_echo": "tp-echo"}
    (g,) = guard_function_tools([tp_echo], gateway, value_at_risk=1.0,
                                capability_map=cm)
    args = json.dumps({"text": "hi"})
    out = asyncio.get_event_loop().run_until_complete(
        g.on_invoke_tool(ctx(args), args))
    assert "echo:hi" in str(out)

    (g_high,) = guard_function_tools([tp_echo], gateway, value_at_risk=5000.0,
                                     capability_map=cm)
    res = asyncio.get_event_loop().run_until_complete(
        g_high.on_invoke_tool(ctx(args), args))
    assert "denied" in str(res)


# ----------------------------------------------------------------------- mcp
def test_mcp_proxy_gates_tool_calls(guild_server, seeded, gateway, tmp_path):
    """Build the proxy Server object and drive its call_tool handler against
    a real downstream MCP server subprocess."""
    import sys
    downstream = tmp_path / "downstream.py"
    downstream.write_text("""
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("downstream")

@mcp.tool()
def tp_echo(text: str) -> str:
    \"\"\"Echo.\"\"\"
    return "echo:" + text

mcp.run()
""")
    from agentguild_trustplane.mcp_proxy import build_proxy

    def drive(proxy):
        async def _run():
            import mcp.types as types
            handler = proxy.request_handlers[types.CallToolRequest]
            req = types.CallToolRequest(
                method="tools/call",
                params=types.CallToolRequestParams(name="tp_echo",
                                                   arguments={"text": "hi"}))
            return await handler(req)
        return asyncio.get_event_loop().run_until_complete(_run())

    # 1. downstream identity VERIFIED (matches the evaluated provider):
    #    the decision applies and the call goes through.
    proxy = build_proxy(gateway, [sys.executable, str(downstream)],
                        value_at_risk=1.0,
                        capability_map={"tp_echo": "tp-echo"},
                        downstream_provider_id=seeded["worker"]["id"],
                        downstream_did=seeded["worker"]["did"])
    result = drive(proxy)
    text = "".join(c.text for c in result.root.content
                   if hasattr(c, "text"))
    assert "echo:hi" in text
    assert gateway.snapshot()["gates"] >= 1

    # 2. NO identity binding: reputation for the evaluated provider must not
    #    authorize an unrelated downstream server -> unknown-counterparty
    #    policy (deny_unknown_agents) denies, labelled unverified.
    proxy_unbound = build_proxy(gateway, [sys.executable, str(downstream)],
                                value_at_risk=1.0,
                                capability_map={"tp_echo": "tp-echo"})
    res2 = drive(proxy_unbound)
    text2 = "".join(c.text for c in res2.root.content if hasattr(c, "text"))
    assert res2.root.isError
    assert "denied" in text2 and '"identity_bound": false' in text2

    # 3. WRONG identity binding (identity substitution) is equally denied.
    proxy_wrong = build_proxy(gateway, [sys.executable, str(downstream)],
                              value_at_risk=1.0,
                              capability_map={"tp_echo": "tp-echo"},
                              downstream_provider_id="agent_imposter",
                              downstream_did="did:key:zImposter")
    res3 = drive(proxy_wrong)
    assert res3.root.isError
