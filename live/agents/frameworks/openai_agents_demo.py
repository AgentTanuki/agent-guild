"""Research Agent built on the OpenAI Agents SDK that uses Agent Guild for discovery.

The agent is given a tool that queries the live Guild for fact-checkers ranked by
reputation. The LLM decides which one to use — no hardcoded service, no human
selection. After "using" it, the agent records a signed attestation.

Run:
    pip install openai-agents
    export OPENAI_API_KEY=...           # for the agent's own reasoning
    export GUILD_URL=http://127.0.0.1:8000
    python frameworks/openai_agents_demo.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentguild import GuildClient  # noqa: E402

GUILD = GuildClient(os.environ.get("GUILD_URL", "http://127.0.0.1:8000"))


def discover_fact_checkers(min_trust: float = 0.0) -> str:
    """Return Guild-ranked fact-checkers as JSON (id, name, trust, price, latency)."""
    results = GUILD.search("fact-check", min_trust=min_trust)
    slim = [
        {
            "id": r["id"], "name": r["name"], "trust": r["trust"],
            "confidence": round(r["confidence"], 2),
            "price_per_call": r["metadata"].get("price_per_call"),
            "est_latency_ms": r["metadata"].get("est_latency_ms"),
        }
        for r in results
    ]
    return json.dumps(slim, indent=2)


def main() -> None:
    try:
        from agents import Agent, Runner, function_tool
    except ImportError:
        print("Install the OpenAI Agents SDK:  pip install openai-agents")
        return

    tool = function_tool(discover_fact_checkers)
    agent = Agent(
        name="Research Agent",
        instructions=(
            "You need claims fact-checked. When you do, call discover_fact_checkers to query "
            "Agent Guild, then choose the fact-checker with the best balance of high trust, low "
            "price, and low latency. Explain which you chose and why."
        ),
        tools=[tool],
    )
    result = Runner.run_sync(
        agent,
        "I need to fact-check a claim about astronomy. Find and select the best fact-checker.",
    )
    print(result.final_output)


if __name__ == "__main__":
    main()
