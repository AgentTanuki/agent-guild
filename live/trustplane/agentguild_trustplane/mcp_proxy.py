"""MCP delegation proxy: put the trust plane between an MCP host and any
downstream MCP server without changing either.

    python -m agentguild_trustplane.mcp_proxy --policy policy.json \
        --downstream-provider-id agent_abc --downstream-did did:key:z6Mk... -- \
        python -m some_downstream_mcp_server

The proxy speaks MCP stdio upstream (to the host — Claude, an IDE, any MCP
client) and spawns the downstream server as a child. It forwards
``tools/list`` verbatim, and INTERCEPTS every ``tools/call``.

Identity binding (corrective pass 2026-07-13): a Guild decision about a
capability is reputation for a SPECIFIC provider — it must never authorize an
unrelated downstream server. The proxy therefore requires the downstream
server's VERIFIED Guild identity binding (``--downstream-provider-id`` +
``--downstream-did``, which must match the signed decision's evaluated
provider), or else the call is labelled UNVERIFIED and evaluated under the
caller's unknown-counterparty policy (decision evidence is NOT applied).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any, Optional

import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .engine import evaluate
from .gateway import Gateway
from .policy import RiskPolicy


def build_proxy(gateway: Gateway, downstream_cmd: list[str],
                value_at_risk: float = 0.0,
                capability_map: dict[str, str] | None = None,
                downstream_provider_id: Optional[str] = None,
                downstream_did: Optional[str] = None) -> Server:
    """A Server whose tool surface mirrors the downstream server, gated.

    ``downstream_provider_id``/``downstream_did``: the downstream server's
    registered Guild identity. When set, a decision only authorizes the call
    if its evaluated provider matches EXACTLY; when unset, decisions never
    apply and the caller's unknown-counterparty policy decides."""
    app: Server = Server("agentguild-gateway-proxy")
    state: dict[str, Any] = {}
    capability_map = capability_map or {}

    async def _downstream() -> ClientSession:
        if "session" in state:
            return state["session"]
        params = StdioServerParameters(command=downstream_cmd[0],
                                       args=downstream_cmd[1:])
        ctx = stdio_client(params)
        read, write = await state.setdefault("_stack", __import__(
            "contextlib").AsyncExitStack()).enter_async_context(ctx)
        session = await state["_stack"].enter_async_context(
            ClientSession(read, write))
        await session.initialize()
        state["session"] = session
        return session

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        session = await _downstream()
        res = await session.list_tools()
        return res.tools

    @app.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]
                        ) -> types.CallToolResult:
        capability = capability_map.get(name, name)
        gate = gateway.gate(capability, value_at_risk,
                            context={"transport": "mcp", "tool": name,
                                     "downstream_provider_id":
                                         downstream_provider_id})
        # IDENTITY BINDING: reputation for provider X never authorizes
        # downstream server Y. The decision applies ONLY when the caller
        # declared the downstream's Guild identity and it matches the
        # evaluated provider; otherwise re-evaluate as unknown counterparty.
        identity_bound = bool(
            downstream_provider_id and downstream_did
            and gate.worker_id == downstream_provider_id
            and gate.provider_did == downstream_did)
        if not identity_bound and gate.decision is not None:
            pol = evaluate(None, gateway.policy, gate.tier,
                           fail_state=gate.channel)
            pol.reasons.append(
                "downstream server has no verified Guild identity binding "
                "to the evaluated provider — decision evidence NOT applied; "
                "unknown-counterparty policy decided this call")
            gate.policy = pol
            gate.allowed = pol.allowed
            gate.meta["identity_bound"] = False
            gate.meta["unverified_downstream"] = True
        else:
            gate.meta["identity_bound"] = identity_bound
        if not gate.allowed:
            gateway.report(gate, "blocked")
            return types.CallToolResult(isError=True, content=[
                types.TextContent(type="text", text=json.dumps({
                    "error": "delegation denied by caller policy",
                    "policy": gate.policy.to_json(),
                    "identity_bound": gate.meta.get("identity_bound", False),
                    "gate_id": gate.gate_id,
                }))])
        session = await _downstream()
        t0 = time.perf_counter()
        try:
            result = await session.call_tool(name, arguments)
            latency = (time.perf_counter() - t0) * 1000.0
            text = "".join(c.text for c in result.content
                           if isinstance(c, types.TextContent))
            gateway.report(gate,
                           "rejected" if result.isError else "accepted",
                           deliverable=text, latency_ms=latency)
            return result   # verbatim, structuredContent preserved
        except Exception as e:
            gateway.report(gate, "rejected",
                           latency_ms=(time.perf_counter() - t0) * 1000.0)
            return types.CallToolResult(isError=True, content=[
                types.TextContent(type="text", text=json.dumps(
                    {"error": str(e), "gate_id": gate.gate_id}))])

    return app


async def run(gateway: Gateway, downstream_cmd: list[str],
              value_at_risk: float,
              downstream_provider_id: Optional[str] = None,
              downstream_did: Optional[str] = None) -> None:
    app = build_proxy(gateway, downstream_cmd, value_at_risk,
                      downstream_provider_id=downstream_provider_id,
                      downstream_did=downstream_did)
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", default=None)
    ap.add_argument("--guild", default=None)
    ap.add_argument("--state-dir", default="~/.agentguild")
    ap.add_argument("--value-at-risk", type=float, default=0.0)
    ap.add_argument("--downstream-provider-id", default=None,
                    help="the downstream server's registered Guild agent id "
                         "(with --downstream-did, binds decisions to it)")
    ap.add_argument("--downstream-did", default=None,
                    help="the downstream server's registered Guild DID")
    ap.add_argument("downstream", nargs=argparse.REMAINDER,
                    help="-- <command to launch the downstream MCP server>")
    args = ap.parse_args()
    cmd = [c for c in args.downstream if c != "--"]
    if not cmd:
        print("usage: ... -- <downstream server command>", file=sys.stderr)
        raise SystemExit(2)
    pol = RiskPolicy.load(args.policy) if args.policy else RiskPolicy()
    kw: dict[str, Any] = {"policy": pol, "state_dir": args.state_dir}
    if args.guild:
        kw["base_url"] = args.guild
    asyncio.run(run(Gateway(**kw), cmd, args.value_at_risk,
                    args.downstream_provider_id, args.downstream_did))


if __name__ == "__main__":
    main()
