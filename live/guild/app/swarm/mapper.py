"""L3 — Machine Discovery Mapper: the machine-readable map of legitimate,
terms-compliant machine ecosystems where AG identities can be discovered.

Adapters are READ-ONLY verification probes against public, documented
endpoints. Registration into ecosystems marked needs_human stays draft-only —
the publisher discovery agent prepares submissions; a human sends them.
Fetched external content is never executed and never mutates the capability
registry (threat model T2)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

# One entry per ecosystem. `verify_urls` are read-only GETs the verifier agent
# may fetch; everything else is descriptive metadata.
ECOSYSTEMS: list[dict[str, Any]] = [
    {
        "id": "mcp_registry_official",
        "name": "Official MCP Registry",
        "protocol": "mcp",
        "registration_method": "server.json + GitHub OIDC workflow "
                               "(.github/workflows/publish-mcp.yml) — automated, permitted",
        "search_method": "GET https://registry.modelcontextprotocol.io/v0/servers?search=<q>",
        "capability_taxonomy": "free-text description + tool list",
        "auth": "none for read",
        "rate_limits": "public API, be polite (verifier: <=2 req/tick)",
        "terms": "public registry; publication via own-namespace server.json only",
        "ag_coverage": "published (io.github.AgentTanuki/agent-guild)",
        "verify_urls": ["https://registry.modelcontextprotocol.io/v0/servers?search=agent-guild"],
        "demand_signals": "search volume not exposed; coverage check only",
    },
    {
        "id": "glama",
        "name": "Glama MCP directory",
        "protocol": "mcp",
        "registration_method": "/.well-known/glama.json + repo auto-index",
        "search_method": "https://glama.ai/mcp/servers (HTML + API)",
        "capability_taxonomy": "tool list, auto-extracted",
        "auth": "none for read",
        "rate_limits": "public site; verifier: 1 req/tick",
        "terms": "listing via published ownership file — permitted",
        "ag_coverage": "published",
        "verify_urls": ["https://glama.ai/mcp/servers/@AgentTanuki/agent-guild"],
        "demand_signals": "none exposed",
    },
    {
        "id": "smithery",
        "name": "Smithery",
        "protocol": "mcp",
        "registration_method": "manual listing (done); NOTE trailing-slash /mcp/ URL",
        "search_method": "https://smithery.ai/search",
        "capability_taxonomy": "tool list",
        "auth": "none for read",
        "rate_limits": "public site; verifier: 1 req/tick",
        "terms": "listed — permitted",
        "ag_coverage": "published (agent-tanuki/agent-guild)",
        "verify_urls": ["https://smithery.ai/server/agent-tanuki/agent-guild"],
        "demand_signals": "usage counter on listing page",
    },
    {
        "id": "a2a_wellknown",
        "name": "A2A well-known discovery (self-hosted)",
        "protocol": "a2a",
        "registration_method": "serve /.well-known/agent-card.json (done)",
        "search_method": "crawlers (a2aregistry, AgentExchange, Agentry) fetch it",
        "capability_taxonomy": "AgentCard skills[]",
        "auth": "none",
        "rate_limits": "n/a (our own surface)",
        "terms": "A2A spec well-known path — permitted",
        "ag_coverage": "published; swarm skills added in Pilot A",
        "verify_urls": [],  # own-domain checks run in-process, not over network
        "demand_signals": "inbound a2a_message events (see /instrumentation)",
    },
    {
        "id": "agentry",
        "name": "Agentry directory (Nostr-based)",
        "protocol": "a2a/nostr",
        "registration_method": "Nostr identity (registered; live/secrets/agentry_credentials.json)",
        "search_method": "directory crawl of agent cards",
        "capability_taxonomy": "profile categories",
        "auth": "nostr keypair",
        "rate_limits": "unknown — treat conservatively",
        "terms": "identity registered; profile updates only via documented flow",
        "ag_coverage": "identity registered",
        "verify_urls": [],
        "demand_signals": "AgenstryBot crawls our /a2a (seen in telemetry)",
    },
    {
        "id": "pulsemcp",
        "name": "PulseMCP",
        "protocol": "mcp",
        "registration_method": "human submission form",
        "search_method": "public directory",
        "capability_taxonomy": "tool list",
        "auth": "none for read",
        "rate_limits": "public site",
        "terms": "needs_human — submission prepared in live/outreach, NOT auto-sent",
        "ag_coverage": "not listed (blocked_needs_human)",
        "verify_urls": [],
        "demand_signals": "none",
    },
    {
        "id": "mcp_so",
        "name": "mcp.so",
        "protocol": "mcp",
        "registration_method": "human submission",
        "search_method": "public directory",
        "capability_taxonomy": "tags",
        "auth": "none for read",
        "rate_limits": "public site",
        "terms": "needs_human — draft-only",
        "ag_coverage": "not listed (blocked_needs_human)",
        "verify_urls": [],
        "demand_signals": "none",
    },
]

# Own-domain machine surfaces the verifier checks in-process (no network,
# no self-request deadlock): path -> required keys/shape marker.
SELF_SURFACES: dict[str, str] = {
    "/.well-known/agent-guild.json": "schema_version",
    "/.well-known/agent-card.json": "protocolVersion",
    "/.well-known/ag-identities/index.json": "identities",
    "/.well-known/agent-guild-did.json": "",
    "/terms.json": "guest_tier",
    "/llms.txt": "",
    "/openapi.json": "paths",
}


def ecosystem_map(store) -> dict:
    health = store.swarm_state.get("adapter_health", {})
    entries = []
    for eco in ECOSYSTEMS:
        h = health.get(eco["id"], {})
        entries.append({**eco,
                        "last_verified": h.get("last_verified"),
                        "adapter_health": h.get("status", "unverified")})
    return {"schema_version": "ag-ecosystem-map/1",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "ecosystems": entries}


def note_adapter_health(store, eco_id: str, status: str, detail: str = "") -> None:
    with store.lock:
        h = store.swarm_state.setdefault("adapter_health", {})
        h[eco_id] = {"status": status, "detail": detail[:300],
                     "last_verified": datetime.now(timezone.utc).isoformat()}
