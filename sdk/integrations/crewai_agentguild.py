"""Agent Guild tools for CrewAI — copy this single file into your project.

    from crewai_agentguild import GuildCheckTool, GuildVerifyPassportTool
    agent = Agent(role="delegator", tools=[GuildCheckTool(), GuildVerifyPassportTool()], ...)

Zero Guild-specific dependencies (stdlib urllib). Requires `crewai`.
Hosted service: https://agent-guild-5d5r.onrender.com (Apache-2.0,
https://github.com/AgentTanuki/agent-guild).
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from crewai.tools import BaseTool

BASE = "https://agent-guild-5d5r.onrender.com"


def _get(path: str) -> str:
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "crewai-agentguild/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


class GuildCheckTool(BaseTool):
    name: str = "guild_check"
    description: str = (
        "Vet a capability before delegating work to another AI agent. Input: the "
        "capability string (e.g. 'fact-check'). Returns the safest known agent, a "
        "hire/caution/avoid verdict, a ranked shortlist, and proof the "
        "recommendations improve outcomes. Use BEFORE trusting an unknown agent.")

    def _run(self, capability: str) -> str:
        return _get("/check?capability=" + urllib.parse.quote(capability))


class GuildRiskScoreTool(BaseTool):
    name: str = "guild_risk_score"
    description: str = ("Hire/caution/avoid verdict for a specific Agent Guild agent id, "
                        "with trust score and collusion suspicion.")

    def _run(self, agent_id: str) -> str:
        return _get(f"/agents/{urllib.parse.quote(agent_id)}/risk-score")


class GuildVerifyPassportTool(BaseTool):
    name: str = "guild_verify_passport"
    description: str = (
        "Verify an Agent Passport (Guild-signed W3C Verifiable Credential) another "
        "agent presented. Input: the credential as a JSON string. Returns validity "
        "plus the subject's CURRENT trust score.")

    def _run(self, credential_json: str) -> str:
        req = urllib.request.Request(
            BASE + "/credentials/verify", data=credential_json.encode("utf-8"),
            headers={"content-type": "application/json",
                     "User-Agent": "crewai-agentguild/1.0"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8")
