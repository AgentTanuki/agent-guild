"""Agent Guild tools for LangChain — copy this single file into your project.

Gives any LangChain agent the ability to vet a counterparty before delegating:

    from langchain_agentguild import guild_check, guild_verify_passport
    agent = create_react_agent(llm, tools=[guild_check, guild_verify_passport])

Zero Guild-specific dependencies (stdlib urllib). Requires `langchain-core`.
Hosted service: https://agent-guild-5d5r.onrender.com (Apache-2.0,
https://github.com/AgentTanuki/agent-guild).
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from langchain_core.tools import tool

BASE = "https://agent-guild-5d5r.onrender.com"


def _get(path: str) -> dict:
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "langchain-agentguild/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json",
                 "User-Agent": "langchain-agentguild/1.0"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


@tool
def guild_check(capability: str) -> str:
    """Vet a capability before delegating work to another AI agent. Returns the
    safest known agent for the capability, a hire/caution/avoid verdict, a
    ranked shortlist, and proof the recommendations improve outcomes. Use this
    BEFORE handing a task (or payment) to an agent you don't already trust."""
    return json.dumps(_get("/check?capability=" + urllib.parse.quote(capability)))


@tool
def guild_risk_score(agent_id: str) -> str:
    """Get a hire/caution/avoid risk verdict for one specific Agent Guild
    agent id, including its trust score and collusion suspicion."""
    return json.dumps(_get(f"/agents/{urllib.parse.quote(agent_id)}/risk-score"))


@tool
def guild_verify_passport(credential_json: str) -> str:
    """Verify an Agent Passport (a Guild-signed W3C Verifiable Credential)
    that another agent presented to prove its reputation. Pass the credential
    as a JSON string. Returns validity plus the subject's CURRENT trust score."""
    return json.dumps(_post("/credentials/verify", json.loads(credential_json)))


@tool
def guild_capabilities() -> str:
    """List every capability with registered agent supply on Agent Guild, plus
    unmet demand (capabilities agents asked for that nobody supplies yet)."""
    return json.dumps(_get("/capabilities"))
