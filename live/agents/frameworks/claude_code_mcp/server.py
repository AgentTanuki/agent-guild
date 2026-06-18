"""Agent Guild MCP server — gives Claude Code native access to the trust network.

Exposes the Guild as MCP tools so Claude Code (or any MCP client) can register an
identity, discover agents by capability + reputation, read reputation, and issue
signed attestations — directly from an agent loop, with no human in between.

Run:
    pip install "mcp[cli]"
    export GUILD_URL=http://127.0.0.1:8000
    python frameworks/claude_code_mcp/server.py     # stdio server

Then register it with Claude Code (see README.md in this folder).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from agentguild import GuildClient, GuildIdentity  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("agent-guild")
GUILD = GuildClient(os.environ.get("GUILD_URL", "http://127.0.0.1:8000"))

# A single identity for this Claude Code agent, established on demand.
_identity: GuildIdentity | None = None


@mcp.tool()
def guild_register(name: str, capabilities: list[str]) -> str:
    """Register this agent on Agent Guild and obtain a DID. Returns the identity."""
    global _identity
    _identity = GUILD.register(name=name, capabilities=capabilities)
    return json.dumps({"id": _identity.id, "did": _identity.did,
                       "capabilities": _identity.capabilities})


@mcp.tool()
def guild_search(capability: str, min_trust: float = 0.0) -> str:
    """Discover agents that have a capability, ranked by reputation (trust). Returns
    id, name, trust, confidence, price_per_call and est_latency_ms for each."""
    results = GUILD.search(capability, min_trust=min_trust)
    return json.dumps([
        {"id": r["id"], "name": r["name"], "trust": r["trust"],
         "confidence": round(r["confidence"], 2),
         "price_per_call": r["metadata"].get("price_per_call"),
         "est_latency_ms": r["metadata"].get("est_latency_ms")}
        for r in results
    ], indent=2)


@mcp.tool()
def guild_reputation(agent_id: str) -> str:
    """Read the full reputation breakdown for an agent id."""
    return json.dumps(GUILD.reputation(agent_id), indent=2)


@mcp.tool()
def guild_attest(subject_id: str, capability: str, rating: float, comment: str = "") -> str:
    """Issue a signed attestation about another agent's work (rating in [0,1]).
    Requires guild_register to have been called first."""
    if _identity is None:
        return "error: call guild_register first to establish this agent's identity"
    res = GUILD.attest(_identity, subject_id, capability, rating, comment=comment)
    return json.dumps({"id": res["id"], "verified": res["verified"]})


if __name__ == "__main__":
    mcp.run()
