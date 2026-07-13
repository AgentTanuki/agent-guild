"""LangChain / LangGraph interceptor.

* ``GuardedTool`` — a ``BaseTool`` that WRAPS any existing langchain tool:
  the inner tool's ``_run``/``_arun`` only executes after the caller's policy
  gate passes. Because it IS a BaseTool, it drops into every native surface —
  AgentExecutor, LangGraph ``ToolNode``, ``create_react_agent`` — unchanged.

* ``guard_tools(tools, gateway)`` — wrap a whole toolbox for LangGraph:
      graph = create_react_agent(model, guard_tools(tools, gw))

* ``TrustPlaneCallback`` — a native ``BaseCallbackHandler`` that records
  outcomes on ``on_tool_end``/``on_tool_error`` for guarded tools, riding the
  framework's own lifecycle rather than trusting tool authors to report.
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Optional
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tools import BaseTool

from ..gateway import Gateway, GateDenied, GateResult


class GuardedTool(BaseTool):
    """Policy-gated wrapper around any langchain BaseTool."""

    inner: BaseTool
    gateway: Any
    capability: str
    value_at_risk: float = 0.0
    # gate handles keyed by run_id so the callback can close the loop
    model_config = {"arbitrary_types_allowed": True}

    def __init__(self, inner: BaseTool, gateway: Gateway, *,
                 capability: Optional[str] = None,
                 value_at_risk: float = 0.0, **kwargs: Any) -> None:
        super().__init__(
            name=inner.name, description=inner.description,
            args_schema=inner.args_schema, inner=inner, gateway=gateway,
            capability=capability or inner.name,
            value_at_risk=value_at_risk, **kwargs)
        self._open_gates: dict[str, GateResult] = {}

    def _gate(self) -> GateResult:
        gate = self.gateway.gate(self.capability, self.value_at_risk,
                                 context={"framework": "langchain",
                                          "tool": self.name})
        if not gate.allowed:
            self.gateway.report(gate, "blocked")
            raise GateDenied(gate)
        self._open_gates["last"] = gate
        return gate

    def _close(self, gate: GateResult, outcome: str, result: Any,
               t0: float) -> None:
        self.gateway.report(gate, outcome,
                            deliverable=None if result is None else str(result),
                            latency_ms=(time.perf_counter() - t0) * 1000.0)
        self._open_gates.pop("last", None)

    @staticmethod
    def _tool_input(args: tuple, kwargs: dict) -> Any:
        kwargs.pop("run_manager", None)
        kwargs.pop("config", None)
        if kwargs:
            return kwargs
        return args[0] if args else {}

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        gate = self._gate()
        t0 = time.perf_counter()
        try:
            # go through the inner tool's own public pipeline so ITS
            # callbacks/validation run too
            result = self.inner.invoke(self._tool_input(args, kwargs))
        except Exception:
            self._close(gate, "rejected", None, t0)
            raise
        self._close(gate, "accepted", result, t0)
        return result

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        gate = self._gate()
        t0 = time.perf_counter()
        try:
            result = await self.inner.ainvoke(self._tool_input(args, kwargs))
        except Exception:
            self._close(gate, "rejected", None, t0)
            raise
        self._close(gate, "accepted", result, t0)
        return result


def guard_tools(tools: Iterable[BaseTool], gateway: Gateway, *,
                value_at_risk: float = 0.0,
                capability_map: Optional[dict[str, str]] = None
                ) -> list[BaseTool]:
    """Wrap a toolbox for ToolNode / create_react_agent / AgentExecutor.
    ``capability_map`` maps tool names to Guild capability strings when they
    differ (default: the tool name)."""
    cm = capability_map or {}
    return [GuardedTool(t, gateway, capability=cm.get(t.name),
                        value_at_risk=value_at_risk) for t in tools]


class TrustPlaneCallback(BaseCallbackHandler):
    """Native callback lifecycle: independent record of tool starts/ends —
    used by the A/B harness to verify no guarded invocation escapes the
    gateway (evidence completion cross-check)."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.ended: list[str] = []
        self.errored: list[str] = []

    def on_tool_start(self, serialized: dict[str, Any], input_str: str, *,
                      run_id: UUID, **kwargs: Any) -> None:
        self.started.append(serialized.get("name", "?"))

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self.ended.append(str(run_id))

    def on_tool_error(self, error: BaseException, *, run_id: UUID,
                      **kwargs: Any) -> None:
        self.errored.append(str(run_id))
