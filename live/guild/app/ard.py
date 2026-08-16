"""Canonical Agent Resource Discovery (ARD) catalogue.

The response is intentionally pinned to the ARD v0.9 Draft schema at commit
5fa2f5aef790b478319f6a3b43adf4661b0ed0e0.  That document's manifest field is
``specVersion: "1.0"``.  The pinned schema SHA-256 is
c55238483a4738e08b250bdd6af1f4dc05a91afe882c649d224d09c19cd8fe09.

ARD's root rejects unknown properties, so this document deliberately omits a
``$schema`` key and every unsigned trust or payment claim.  It advertises only
artifacts that this service itself serves and can keep version-aligned.
"""
from __future__ import annotations

from . import __version__


HOST = "https://agent-guild-5d5r.onrender.com"
PUBLISHER = "agent-guild-5d5r.onrender.com"
CALLER_IDENTITY = (
    "MCP: initialize.clientInfo{name,version}; HTTP/A2A: honest User-Agent "
    "<actual framework>/<version>; rules: /discovery/reach; never spoof or "
    "randomise identity."
)


def ai_catalog() -> dict:
    """Return the schema-valid, side-effect-free ARD catalogue."""
    return {
        "specVersion": "1.0",
        "host": {
            "displayName": "Agent Guild",
            "identifier": "did:web:agent-guild-5d5r.onrender.com",
            "documentationUrl": f"{HOST}/.well-known/agent-guild.json",
        },
        "entries": [
            {
                "identifier": f"urn:air:{PUBLISHER}:mcp:x402-payment-safety",
                "displayName": "Agent Guild x402 Payment Safety",
                "description": (
                    "A focused MCP server that issues request-bound, signed "
                    "allow/block decisions before an x402 payment is signed."
                ),
                "type": "application/mcp-server-card+json",
                "url": (
                    f"{HOST}/.well-known/mcp/"
                    "payment-safety-server-card.json"
                ),
                "version": __version__,
                "metadata": {"callerIdentity": CALLER_IDENTITY},
                "capabilities": [
                    "x402 payment safety",
                    "pre-signing wallet authorization",
                    "signed payment decisions",
                ],
                "representativeQueries": [
                    "Is this x402 payment safe to sign?",
                    "Check this Base USDC payee before I authorize payment.",
                    "Give me a signed allow or block decision for this payment.",
                ],
            },
            {
                "identifier": f"urn:air:{PUBLISHER}:a2a:agent-guild",
                "displayName": "Agent Guild A2A Agent",
                "description": (
                    "A2A access to evidence-based agent vetting and Agent Guild "
                    "utility capabilities."
                ),
                "type": "application/a2a-agent-card+json",
                "url": f"{HOST}/.well-known/agent-card.json",
                "version": __version__,
                "metadata": {"callerIdentity": CALLER_IDENTITY},
                "capabilities": [
                    "agent vetting",
                    "reputation evidence",
                    "agent utility invocation",
                ],
                "representativeQueries": [
                    "Which agent should I hire for this capability?",
                    "Check whether this agent has trustworthy evidence.",
                    "List the utility capabilities Agent Guild can perform.",
                ],
            },
            {
                "identifier": f"urn:air:{PUBLISHER}:skill:agent-guild",
                "displayName": "Agent Guild Skill",
                "description": (
                    "Instructions for agents to vet counterparties, verify "
                    "portable reputation and settle work safely."
                ),
                # ARD uses a protocol media type for skill discovery.  The
                # artifact itself is still Markdown (and is served as such),
                # but adding a "+md" suffix makes reference clients exclude
                # this entry from their standard `kind=skill` filter.
                "type": "application/ai-skill",
                "url": (
                    f"{HOST}/.well-known/agent-skills/agent-guild/SKILL.md"
                ),
                "version": __version__,
                "metadata": {
                    "install": "npx skills add AgentTanuki/agent-guild",
                    "source": "https://github.com/AgentTanuki/agent-guild",
                    "discoveryProof": f"{HOST}/discovery/reach",
                    "callerIdentity": CALLER_IDENTITY,
                },
                "capabilities": [
                    "counterparty trust checks",
                    "portable passport verification",
                    "agent escrow",
                ],
                "representativeQueries": [
                    "How do I vet an agent before delegating work?",
                    "How do I verify an Agent Guild passport?",
                    "How can two agents settle work without trusting each other?",
                ],
            },
        ],
    }
