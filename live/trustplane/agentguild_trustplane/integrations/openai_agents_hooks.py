"""OpenAI Agents SDK interceptor.

* ``guard_function_tools(tools, gateway)`` — intercepts each
  ``FunctionTool.on_invoke_tool`` (the SDK's single tool-execution choke
  point) with the policy gate. The agent/runner code is unchanged; denial
  returns a structured policy error the model can read (or raises in strict
  mode), and every invocation ends in a signed outcome.

* ``TrustPlaneRunHooks`` — a native ``RunHooks`` implementation recording
  tool lifecycle spans (on_tool_start/on_tool_end) as an independent
  cross-check that no guarded tool ran ungated.
"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Any, Iterable, Optional

from agents import FunctionTool, RunHooks

from ..gateway import Gateway, GateDenied


def guard_function_tools(tools: Iterable[FunctionTool], gateway: Gateway, *,
                         value_at_risk: float = 0.0,
                         strict: bool = False,
                         capability_map: Optional[dict[str, str]] = None
                         ) -> list[FunctionTool]:
    """Return gated copies of ``tools`` (FunctionTool is a dataclass; we
    replace on_invoke_tool with the gated closure)."""
    guarded: list[FunctionTool] = []
    cm = capability_map or {}
    for tool in tools:
        original = tool.on_invoke_tool
        cap = cm.get(tool.name, tool.name)

        async def gated(ctx: Any, args_json: str, *,
                        _orig: Any = original, _cap: str = cap) -> Any:
            gate = gateway.gate(_cap, value_at_risk,
                                context={"framework": "openai-agents",
                                         "tool": _cap})
            if not gate.allowed:
                gateway.report(gate, "blocked")
                if strict:
                    raise GateDenied(gate)
                return json.dumps({
                    "error": "delegation denied by caller policy",
                    "policy": gate.policy.to_json(),
                    "gate_id": gate.gate_id})
            t0 = time.perf_counter()
            try:
                result = await _orig(ctx, args_json)
            except Exception:
                gateway.report(gate, "rejected",
                               latency_ms=(time.perf_counter() - t0) * 1000.0)
                raise
            gateway.report(gate, "accepted", deliverable=str(result),
                           latency_ms=(time.perf_counter() - t0) * 1000.0)
            return result

        guarded.append(replace(tool, on_invoke_tool=gated))
    return guarded


class TrustPlaneRunHooks(RunHooks):
    """Native RunHooks lifecycle — independent span log for the cross-check."""

    def __init__(self) -> None:
        self.tool_spans: list[dict[str, Any]] = []

    async def on_tool_start(self, context: Any, agent: Any,
                            tool: Any) -> None:
        self.tool_spans.append({"tool": getattr(tool, "name", "?"),
                                "started_at": time.time(), "ended_at": None})

    async def on_tool_end(self, context: Any, agent: Any, tool: Any,
                          result: str) -> None:
        for span in reversed(self.tool_spans):
            if span["tool"] == getattr(tool, "name", "?") and \
               span["ended_at"] is None:
                span["ended_at"] = time.time()
                break
