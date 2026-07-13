"""CrewAI interceptor: gate at BaseTool.run, record off the native event bus.

Two cooperating pieces (both are lifecycle hooks, not opt-in lookup tools):

* ``guard_tool(tool, gateway, ...)`` — returns the SAME tool instance with its
  ``run`` method intercepted. Every invocation is gated by the caller's policy
  BEFORE the tool body executes; a denial raises ``GateDenied`` (enforce) or
  proceeds flagged (monitor). Delegation-class tools should declare
  ``capability`` /``value_at_risk``; defaults derive from the tool name.

* ``TrustPlaneListener`` — a native ``BaseEventListener`` on
  ``crewai_event_bus`` that records a signed outcome for every
  ToolUsageFinished/ToolUsageError event of guarded tools, so evidence
  completion does not depend on the tool author remembering to report.

Usage:
    from agentguild_trustplane.integrations.crewai_hooks import (
        guard_tools, TrustPlaneListener)
    tools = guard_tools([search_tool, delegate_tool], gateway)
    listener = TrustPlaneListener(gateway)      # attaches on construction
    crew = Crew(agents=[...], tasks=[...])      # unchanged
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Optional

from crewai.events import BaseEventListener, crewai_event_bus
from crewai.events.types.tool_usage_events import (ToolUsageErrorEvent,
                                                   ToolUsageFinishedEvent)

from ..gateway import Gateway, GateDenied

_GUARD_ATTR = "_agentguild_gate"


def guard_tool(tool: Any, gateway: Gateway, *,
               capability: Optional[str] = None,
               value_at_risk: float = 0.0) -> Any:
    """Intercept ``tool.run`` with a policy gate. Returns the same instance."""
    cap = capability or getattr(tool, "name", tool.__class__.__name__)
    original_run = tool.run

    def gated_run(*args: Any, **kwargs: Any) -> Any:
        gate = gateway.gate(cap, value_at_risk,
                            context={"framework": "crewai",
                                     "tool": getattr(tool, "name", cap)})
        object.__setattr__(tool, _GUARD_ATTR, gate)   # pydantic-safe stash
        if not gate.allowed:
            gateway.report(gate, "blocked")
            raise GateDenied(gate)
        t0 = time.perf_counter()
        try:
            result = original_run(*args, **kwargs)
        except Exception:
            gateway.report(gate, "rejected",
                           latency_ms=(time.perf_counter() - t0) * 1000.0)
            raise
        # success outcome is recorded by TrustPlaneListener off the event bus
        # when attached; record here too if no listener claimed it.
        if not getattr(tool, "_agentguild_listener_active", False):
            gateway.report(gate, "accepted", deliverable=str(result),
                           latency_ms=(time.perf_counter() - t0) * 1000.0)
        return result

    object.__setattr__(tool, "run", gated_run)
    return tool


def guard_tools(tools: Iterable[Any], gateway: Gateway, *,
                value_at_risk: float = 0.0) -> list[Any]:
    return [guard_tool(t, gateway, value_at_risk=value_at_risk) for t in tools]


class TrustPlaneListener(BaseEventListener):
    """Native CrewAI event-bus listener: signed outcome per tool usage."""

    def __init__(self, gateway: Gateway,
                 guarded_tools: Optional[Iterable[Any]] = None) -> None:
        self.gateway = gateway
        self._tools = {getattr(t, "name", None): t for t in (guarded_tools or [])}
        super().__init__()
        for t in self._tools.values():
            object.__setattr__(t, "_agentguild_listener_active", True)

    def setup_listeners(self, bus: Any = None) -> None:
        bus = bus or crewai_event_bus

        @bus.on(ToolUsageFinishedEvent)
        def _finished(source: Any, event: ToolUsageFinishedEvent) -> None:
            self._record(event.tool_name, "accepted",
                         deliverable=str(getattr(event, "output", "")))

        @bus.on(ToolUsageErrorEvent)
        def _errored(source: Any, event: ToolUsageErrorEvent) -> None:
            self._record(event.tool_name, "rejected")

    def _record(self, tool_name: str, outcome: str,
                deliverable: Optional[str] = None) -> None:
        tool = self._tools.get(tool_name)
        gate = getattr(tool, _GUARD_ATTR, None) if tool is not None else None
        if gate is None:
            return  # not a guarded tool — none of our business
        self.gateway.report(gate, outcome, deliverable=deliverable)
        object.__setattr__(tool, _GUARD_ATTR, None)
