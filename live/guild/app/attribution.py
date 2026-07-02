"""Single source of truth for 'is this a GENUINE external agent?'.

Distribution's one success metric is an agent WE DON'T OPERATE actually using the
service. The hard part is not fooling ourselves: our own verification traffic (a
`curl`/`python-urllib` call from a test run) hits the same endpoints a real agent
would. A naive "non-empty user-agent => genuine external" rule is wrong — it counts
our own tooling. This module defines the honest, conservative rule, used by the live
instrumentation AND the standalone detector so they can never disagree.

A call counts as genuine external ONLY if it is not first-party AND it IDENTIFIES
ITSELF AS AN AGENT in a way our own traffic does not:
  * an MCP client that named itself in the handshake (`mcp:<client>`) and is not one
    of ours, OR
  * a recognised agent-framework user-agent (httpx/langchain/openai/... ) that is not
    bare tooling.

Deliberately NOT sufficient (all indistinguishable from our own traffic, so counting
them would fool us):
  * bare tooling — `curl`, `wget`, `python-urllib`, empty UA (our verification calls);
  * the legacy unattributable `mcp/remote`;
  * a bare registered billing key (`ak_`/`sk_`) with no agent-identifying UA — our own
    pre-tagging seed/test accounts look exactly like this (real keys, empty UA), so a
    key alone is NOT proof of a third party.
Erring toward UNDER-counting is correct: better to miss a real agent for a day than
to falsely announce adoption. When a genuine agent arrives it will present an MCP
client id or a framework UA, and we'll see it.
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

# Known first-party incidents: OUR OWN traffic that slipped past first-party
# tagging (e.g. a maintainer test that forgot the X-Guild-Source header) and
# would otherwise read as genuine external. Each entry is deliberately narrow —
# an exact UA within a bounded time window — and documents why, so this can
# never silently hide a real agent. The same UA OUTSIDE the window still counts.
KNOWN_FIRST_PARTY_INCIDENTS: list[dict[str, str]] = [
    {
        "ua": "crewai-tools-agentguild/1.0",
        "from": "2026-07-02T08:00:00+00:00",
        "to": "2026-07-02T09:00:00+00:00",
        "reason": "Maintainer verification of the crewAI PR #6429 review fixes "
                  "(run from our own sandbox against prod); the X-Guild-Source "
                  "first-party header was omitted by mistake.",
    },
]


def _is_known_first_party_incident(event: dict[str, Any]) -> bool:
    ua = (event.get("ua", event.get("user_agent")) or "").strip()
    at = event.get("at") or ""
    for inc in KNOWN_FIRST_PARTY_INCIDENTS:
        if ua == inc["ua"] and inc["from"] <= at <= inc["to"]:
            return True
    return False


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
    if _is_known_first_party_incident(event):
        return False
    ua = (event.get("ua", event.get("user_agent")) or "").strip()

    # A self-identified MCP client that isn't one of ours.
    client = _mcp_client(ua)
    if client is not None:
        return client not in OURS_MCP_CLIENTS
    # A recognised agent-framework UA — but never bare tooling.
    if ua and not TOOLING_UA_RE.search(ua) and FRAMEWORK_RE.search(ua):
        return True
    # Everything else (empty/tooling UA, mcp/remote, or a bare registered key with no
    # agent-identifying UA) is indistinguishable from our own traffic — NOT genuine.
    return False


def attribution_class(event: dict[str, Any]) -> str:
    """A human/agent-readable label for why an event is (not) genuine external."""
    if event.get("fp", event.get("first_party")):
        return "first_party"
    if _is_known_first_party_incident(event):
        return "first_party_incident"
    if is_genuine_external(event):
        return "genuine_external"
    ua = (event.get("ua", event.get("user_agent")) or "").strip()
    if ua == "mcp/remote":
        return "unattributable_mcp"
    if not ua or TOOLING_UA_RE.search(ua):
        return "tooling_or_ours"      # curl/urllib/empty — looks like our own tests
    return "unrecognised_external"
