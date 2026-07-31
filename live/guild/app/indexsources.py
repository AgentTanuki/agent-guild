"""Lawful public-registry adapters for the trust index.

WHAT THIS MAY DO
  Read documented, public, read-only registry APIs — the same endpoints a
  browser or any other client hits — at a bounded rate, with a truthful User-
  Agent that identifies us and links to what we do with the data.

WHAT THIS MAY NOT DO, EVER
  Scrape behind authentication, ignore robots directives, evade rate limits,
  impersonate another client, or use any private/undocumented endpoint. The
  mandate is explicit: no Terms-of-Service circumvention, no deceptive traffic.
  If an adapter needs a credential to work, it does not ship — it stops and
  says so.

WHY BOUNDED HARD
  An autonomous ingest loop pointed at someone else's infrastructure is, by
  construction, one bug away from being abuse. Every adapter here is capped in
  page count and total records per run, has a short timeout, and fails silent-
  and-empty rather than retrying aggressively. Being a bad citizen would also
  destroy the only asset this product has, which is being the party whose
  measurements can be trusted.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional

#: Truthful, contactable identification. Never impersonates a browser.
USER_AGENT = ("agent-guild-index/1.0 (+https://agent-guild-5d5r.onrender.com/"
              "index; public trust index; contact via the agent card)")

TIMEOUT_S = 12.0
MAX_PAGES = 3
MAX_RECORDS_PER_RUN = 200


def enabled() -> bool:
    """Ingest is OFF unless explicitly enabled.

    Default-off is deliberate: outbound traffic to third-party infrastructure
    should never start because a container restarted."""
    return (os.environ.get("GUILD_INDEX_INGEST") or "0").strip() == "1"


def _get_json(url: str, timeout: float = TIMEOUT_S) -> Optional[Any]:
    """One bounded public GET. Never raises, never retries hard."""
    req = urllib.request.Request(url, headers={
        "accept": "application/json", "user-agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
            ValueError, OSError):
        return None


# --------------------------------------------------------------------------
# Adapter: the official MCP Registry (public, documented, read-only)
# --------------------------------------------------------------------------
MCP_REGISTRY = "https://registry.modelcontextprotocol.io/v0/servers"


def from_mcp_registry(limit: int = 100) -> list[dict[str, Any]]:
    """Public MCP servers with a REMOTE endpoint.

    Only remote servers are indexed. A server distributed as an npm/PyPI
    package has no endpoint to call, so we could never hold an observation of
    it — and an index entry we can never observe is exactly the listing-shaped
    filler this product exists to replace."""
    out: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(MAX_PAGES):
        url = f"{MCP_REGISTRY}?limit=50" + (f"&cursor={cursor}" if cursor else "")
        page = _get_json(url)
        if not isinstance(page, dict):
            break
        for row in page.get("servers", []):
            server = row.get("server") if isinstance(row, dict) else None
            if not isinstance(server, dict):
                continue
            meta = (row.get("_meta") or {}).get(
                "io.modelcontextprotocol.registry/official") or {}
            if meta.get("status") not in (None, "active"):
                continue
            if meta.get("isLatest") is False:
                continue          # superseded versions are not separate services
            for remote in server.get("remotes") or []:
                endpoint = (remote or {}).get("url")
                if not endpoint:
                    continue
                out.append({
                    "endpoint": endpoint,
                    "source": "mcp_registry",
                    "declared": {
                        "name": server.get("name"),
                        "description": (server.get("description") or "")[:400],
                        "capabilities": [],
                        "protocol": "mcp",
                        "version": server.get("version"),
                        "website": server.get("websiteUrl"),
                    },
                })
                if len(out) >= min(limit, MAX_RECORDS_PER_RUN):
                    return out
        cursor = (page.get("metadata") or {}).get("nextCursor") or ""
        if not cursor:
            break
    return out


# --------------------------------------------------------------------------
# Adapter: our OWN demand/candidate surface (already lawful, already ours)
# --------------------------------------------------------------------------
def from_local_agents(store: Any, limit: int = 200) -> list[dict[str, Any]]:
    """Endpoints already declared to the Guild by registered agents.

    These arrive with an ownership signal we can trust — `first_party` is set
    by an admin-gated deterministic control, never inferred from a name or a
    User-Agent. That matters because ownership is what separates a growth
    metric from self-traffic."""
    out: list[dict[str, Any]] = []
    for agent_id, rec in list(getattr(store, "agents", {}).items())[:limit * 2]:
        endpoint = ((rec.get("metadata") or {}).get("endpoint") or "").strip()
        if not endpoint:
            continue
        out.append({
            "endpoint": endpoint,
            "source": "guild_registration",
            "did": rec.get("did") or "",
            "first_party": bool(rec.get("first_party")),
            "declared": {
                "name": rec.get("name"),
                "capabilities": rec.get("capabilities", []),
                "protocol": "a2a",
                "agent_id": agent_id,
            },
        })
        if len(out) >= limit:
            break
    return out


def collect(store: Any, *, include_remote: bool = True) -> list[dict[str, Any]]:
    """All records for one ingest run, bounded in total.

    Local registrations are collected unconditionally (they are already ours);
    remote public registries only when ingest is explicitly enabled."""
    records = from_local_agents(store)
    if include_remote and enabled():
        records += from_mcp_registry()
    return records[:MAX_RECORDS_PER_RUN]
