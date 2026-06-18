"""Research crew built on CrewAI that uses Agent Guild for trust-based discovery.

A CrewAI agent is equipped with a tool that queries the live Guild. The crew
decides, autonomously, which fact-checker to delegate to based on reputation.

Run:
    pip install crewai
    export OPENAI_API_KEY=...
    export GUILD_URL=http://127.0.0.1:8000
    python frameworks/crewai_demo.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentguild import GuildClient  # noqa: E402

GUILD = GuildClient(os.environ.get("GUILD_URL", "http://127.0.0.1:8000"))


def _search(capability: str = "fact-check", min_trust: float = 0.0) -> str:
    results = GUILD.search(capability, min_trust=min_trust)
    return json.dumps([
        {"id": r["id"], "name": r["name"], "trust": r["trust"],
         "price_per_call": r["metadata"].get("price_per_call"),
         "est_latency_ms": r["metadata"].get("est_latency_ms")}
        for r in results
    ], indent=2)


def main() -> None:
    try:
        from crewai import Agent, Task, Crew
        from crewai.tools import tool
    except ImportError:
        print("Install CrewAI:  pip install crewai")
        return

    @tool("guild_search")
    def guild_search(capability: str, min_trust: float = 0.0) -> str:
        """Query Agent Guild for agents with a capability, ranked by reputation."""
        return _search(capability, min_trust)

    researcher = Agent(
        role="Research Agent",
        goal="Get claims fact-checked by the most trustworthy, cost-effective agent available.",
        backstory="You rely on Agent Guild to discover and vet fact-checkers before delegating.",
        tools=[guild_search],
        verbose=True,
    )
    task = Task(
        description=(
            "Use guild_search to find fact-checkers on Agent Guild, then pick the one with the "
            "best trade-off of trust, price, and latency. State your choice and reasoning."
        ),
        expected_output="The chosen fact-checker's id and a short justification.",
        agent=researcher,
    )
    crew = Crew(agents=[researcher], tasks=[task], verbose=True)
    print(crew.kickoff())


if __name__ == "__main__":
    main()
