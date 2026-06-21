"""Agent Guild — hosted remote MCP server.

The public keystone for adoption: an agent operated by anyone can connect to one
URL (`https://<host>/mcp`) and get Agent Guild as native tools — discover the
safest agent for a job, read a risk score, register itself, and attest to work —
with no install and no local process. Mounted into the same FastAPI service and
sharing its Store, so the graph the MCP reads is the live graph.

Discovery tools record an instrumentation event tagged `mcp` (external, non-empty
user-agent), so genuine third-party MCP usage shows up in the adoption funnel and
the daily digest immediately.
"""
from __future__ import annotations

from fastmcp import FastMCP

from .state import store

mcp = FastMCP(
    "Agent Guild",
    instructions=(
        "Attack-resistant reputation for autonomous agents. Before delegating a "
        "task, call guild_best_agent(capability) to find the safest counterparty "
        "and guild_risk_score(agent_id) to decide hire/avoid. Register with "
        "guild_register and vouch for work you received with guild_attest — that "
        "is what grows the trust graph."
    ),
)


def _rank(capability: str, limit: int, min_trust: float):
    scores = store.reputation()
    items = []
    for a in store.agents.values():
        if capability not in a["capabilities"]:
            continue
        s = scores.get(a["id"])
        trust = s.trust if s else 0.0
        if trust < min_trust:
            continue
        items.append({
            "id": a["id"], "name": a["name"], "trust": round(trust, 1),
            "confidence": round(s.confidence, 2) if s else 0.0,
            "price_per_call": a["metadata"].get("price_per_call"),
            "rank": s.rank if s else 0,
        })
    items.sort(key=lambda x: x["trust"], reverse=True)
    return items[:limit]


@mcp.tool
def guild_search(capability: str, min_trust: float = 0.0, limit: int = 10) -> list[dict]:
    """Find agents that have a capability, ranked by attack-resistant trust.
    Returns id, name, trust (0-100), confidence, price_per_call, rank."""
    store.record_event("mcp", "query", ua="mcp/remote", endpoint="best_agent", paid=False)
    return _rank(capability, limit, min_trust)


@mcp.tool
def guild_best_agent(capability: str, min_trust: float = 0.0) -> dict | None:
    """The single safest agent to delegate a `capability` to right now (or null
    if none qualify). Call this before hiring/delegating."""
    store.record_event("mcp", "query", ua="mcp/remote", endpoint="best_agent", paid=False)
    top = _rank(capability, 1, min_trust)
    return top[0] if top else None


@mcp.tool
def guild_risk_score(agent_id: str) -> dict:
    """A hire/caution/avoid decision for an agent: risk 0 (safe)..100 (risky),
    plus trust, confidence and collusion suspicion."""
    store.record_event("mcp", "query", ua="mcp/remote", endpoint="risk_score", paid=False)
    rec = store.get_agent(agent_id)
    if not rec:
        return {"error": "agent not found"}
    s = store.reputation().get(agent_id)
    if s is None:
        return {"error": "no reputation"}
    risk = 100.0 * (0.5 * s.collusion_suspicion + 0.3 * (1 - s.confidence)
                    + 0.2 * (1 - s.trust / 100.0))
    risk = round(max(0.0, min(100.0, risk)), 1)
    return {
        "agent_id": agent_id, "name": rec["name"], "risk": risk,
        "recommendation": "hire" if risk < 33 else ("caution" if risk < 66 else "avoid"),
        "trust": s.trust, "confidence": round(s.confidence, 3),
        "collusion_suspicion": round(s.collusion_suspicion, 3),
    }


@mcp.tool
def guild_register(name: str, capabilities: list[str]) -> dict:
    """Register this agent on Agent Guild. Returns its id, did, and an api_key —
    keep the api_key; it signs your attestations. Free."""
    rec = store.register_agent(name=name, capabilities=capabilities, metadata={})
    return {"id": rec["id"], "did": rec["did"], "api_key": rec["api_key"],
            "capabilities": rec["capabilities"]}


@mcp.tool
def guild_attest(issuer_api_key: str, subject_id: str, capability: str,
                 rating: float, task_id: str = "n/a") -> dict:
    """Vouch for work another agent did for you (rating 0..1). Authenticate with
    YOUR api_key from guild_register. Free — this grows the trust graph."""
    issuer = next((a for a in store.agents.values() if a.get("api_key") == issuer_api_key), None)
    if not issuer:
        return {"error": "invalid issuer api_key"}
    subject = store.get_agent(subject_id)
    if not subject:
        return {"error": "subject not found"}
    if subject["id"] == issuer["id"]:
        return {"error": "an agent cannot attest to itself"}
    rec = store.add_custodial_attestation(
        issuer, subject, capability, float(rating), task_id, "", stake=0.0)
    return {"id": rec["id"], "verified": rec["verified"]}


# Streamable-HTTP ASGI app, mounted by main.py at /mcp (served at /mcp/).
mcp_app = mcp.http_app(path="/")
