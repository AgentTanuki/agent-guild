"""Single source of truth for 'is this a GENUINE external agent?'.

Distribution's one success metric is an agent WE DON'T OPERATE actually using the
service. The hard part is not fooling ourselves: our own verification traffic (a
`curl`/`python-urllib` call from a test run) hits the same endpoints a real agent
would. A naive "non-empty user-agent => genuine external" rule is wrong — it counts
our own tooling. This module defines the honest, conservative rule, used by the live
instrumentation AND the standalone detector so they can never disagree.

A call counts as genuine external ONLY if it is not first-party AND it carries an
ATTRIBUTABLE, non-tooling identity:
  * a registered billing key (a real account actor, not `anon`/`mcp`), OR
  * an MCP client that named itself in the handshake (`mcp:<client>`) and is not one
    of ours, OR
  * a recognised agent-framework user-agent (httpx/langchain/openai/... ) that is not
    bare tooling.
Bare tooling (`curl`, `wget`, `python-urllib`, empty UA) with an anonymous actor, and
the legacy unattributable `mcp/remote`, are explicitly NOT genuine — that is exactly
what our own tests look like.
"""
from __future__ import annotations

import re
from typing import Any

FRAMEWORK_RE = re.compile(
    r"(httpx|aiohttp|langchain|openai|anthropic|claude|llamaindex|crewai|autogen|"
    r"semantic-kernel|node-fetch|undici|axios|okhttp|go-http-client|reqwest|"
    r"cursor|cline|continue|windsurf|cody|dify|n8n|flowise)", re.I)

# Bare tooling — indistinguishable from our own verification calls. NOT genuine.
TOOLING_UA_RE = re.compile(r"^\s*$|curl|wget|python-urllib|python-requests|libwww|"
                           r"httpie|postman|insomnia|go-http-client/1\.1$", re.I)

# MCP clients we operate ourselves — excluded from the genuine-external signal.
OURS_MCP_CLIENTS = {
    "verify", "healthcheck", "fastmcp", "fastmcp-client", "mcp", "client",
    "agent-guild", "agentguild", "python", "node",
}


def _mcp_client(ua: str) -> str | None:
    if not ua.startswith("mcp:"):
        return None
    return ua[4:].split("/", 1)[0].strip().lower() or None


def is_genuine_external(event: dict[str, Any]) -> bool:
    """True iff `event` is attributable to an agent we do not operate.

    Accepts either the internal event shape (keys `fp`, `ua`, `key`) or the public
    feed shape (keys `first_party`, `user_agent`, `actor`)."""
    first_party = event.get("fp", event.get("first_party"))
    if first_party:
        return False
    ua = (event.get("ua", event.get("user_agent")) or "").strip()
    actor = (event.get("key", event.get("actor")) or "anon")

    # A real, registered billing key acting (not anon, not the generic 'mcp' actor).
    if actor not in ("anon", "mcp", "") and (actor.startswith("ak_") or actor.startswith("sk_")):
        return True
    # A self-identified MCP client that isn't one of ours.
    client = _mcp_client(ua)
    if client is not None:
        return client not in OURS_MCP_CLIENTS
    # A recognised framework UA — but never bare tooling.
    if ua and not TOOLING_UA_RE.search(ua) and FRAMEWORK_RE.search(ua):
        return True
    return False


def attribution_class(event: dict[str, Any]) -> str:
    """A human/agent-readable label for why an event is (not) genuine external."""
    if event.get("fp", event.get("first_party")):
        return "first_party"
    if is_genuine_external(event):
        return "genuine_external"
    ua = (event.get("ua", event.get("user_agent")) or "").strip()
    if ua == "mcp/remote":
        return "unattributable_mcp"
    if not ua or TOOLING_UA_RE.search(ua):
        return "tooling_or_ours"      # curl/urllib/empty — looks like our own tests
    return "unrecognised_external"
