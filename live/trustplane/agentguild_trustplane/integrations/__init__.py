"""Real lifecycle interceptors — not lookup wrappers.

Each module hooks its framework's native tool/delegation lifecycle so the
trust plane runs whether or not the model 'decides' to consult it:

  crewai_hooks          guard_tool()/guard_tools() intercept BaseTool.run;
                        TrustPlaneListener records outcomes off the native
                        crewai_event_bus (ToolUsageFinished/Error events)
  langchain_hooks       GuardedTool wraps any langchain BaseTool's _run/_arun;
                        guard_tools() feeds LangGraph ToolNode/create_react_agent;
                        TrustPlaneCallback records outcomes via the native
                        callback lifecycle (on_tool_start/end/error)
  openai_agents_hooks   guard_function_tools() intercepts FunctionTool.
                        on_invoke_tool; TrustPlaneRunHooks records outcomes
                        via the native RunHooks lifecycle

Supported (pinned) versions: see pins.py. All hooks degrade honestly: a
gateway outage triggers the caller policy's fail mode, never a silent pass.
"""
from .pins import SUPPORTED, check_pins  # noqa: F401
