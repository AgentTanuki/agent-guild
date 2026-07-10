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

import hashlib
import re
from typing import Any, Mapping, Optional

FRAMEWORK_RE = re.compile(
    r"(httpx|aiohttp|langchain|openai|anthropic|claude|llamaindex|crewai|autogen|"
    r"semantic-kernel|node-fetch|undici|axios|okhttp|go-http-client|reqwest|"
    r"cursor|cline|continue|windsurf|cody|dify|n8n|flowise)", re.I)

# Bare tooling — indistinguishable from our own verification calls. NOT genuine.
# `guild-ops-check` is our own scheduled ops probe and is named here explicitly
# so advertised telemetry (discovery_stats) can never count our own heartbeat
# as external demand.
TOOLING_UA_RE = re.compile(r"^\s*$|curl|wget|python-urllib|python-requests|libwww|"
                           r"httpie|postman|insomnia|guild-ops-check|"
                           r"go-http-client/1\.1$", re.I)

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
    {
        "ua": "mcp:probe/1",
        "from": "2026-07-10T07:00:00+00:00",
        "to": "2026-07-10T13:00:00+00:00",
        "reason": "Pilot A cold-discovery audit (2026-07-10): a clean-context "
                  "test client completed the MCP handshake with clientInfo name "
                  "'probe' and called guild_check. It is our own test system; "
                  "by design it sent no X-Guild-Source header, so without this "
                  "entry it reads as a genuine external MCP client.",
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

    # Our own self-identified test harnesses and registry/uptime crawlers are
    # never genuine external — found live 2026-07-10: the MCP verification
    # battery (UA mcp:pilot-a-audit/1) was correctly counted AG_TEST by
    # caller_class but still leaked into the genuine_external headline because
    # this function never consulted those rules.
    if AG_TEST_UA_RE.search(ua) or CRAWLER_UA_RE.search(ua):
        return False

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
    if AG_TEST_UA_RE.search(ua):
        return "ag_test"              # our own self-identified harnesses
    if CRAWLER_UA_RE.search(ua):
        return "registry_crawler"     # indexes manifests, never an agent
    if ua == "mcp/remote":
        return "unattributable_mcp"
    if not ua or TOOLING_UA_RE.search(ua):
        return "tooling_or_ours"      # curl/urllib/empty — looks like our own tests
    return "unrecognised_external"


# ---------------------------------------------------------------------------
# Explicit caller classes (Pilot A instrumentation audit, 2026-07-10).
#
# `attribution_class` answers "why is this (not) genuine external?".
# `caller_class` answers the operational question "WHO is calling?", with a
# closed 7-value taxonomy so growth metrics can be filtered by construction:
# only EXTERNAL_* classes may ever feed external-growth reporting; a registry
# crawler fetching a manifest is never an engaged external agent.
# ---------------------------------------------------------------------------

CALLER_CLASSES = (
    "AG_INTERNAL", "AG_TEST", "REGISTRY_CRAWLER",
    "EXTERNAL_UNKNOWN", "EXTERNAL_VERIFIED", "EXTERNAL_MEMBER", "OPERATOR",
)

# Registry / search-engine / uptime crawlers: they index manifests, they do
# not perform tasks. Matched anywhere in the UA, case-insensitive.
CRAWLER_UA_RE = re.compile(
    r"(glama|smithery|modelcontextprotocol|a2aregistry|crawler|spider|"
    r"bingbot|googlebot|gptbot|claudebot|ccbot|censys|shodan|"
    r"uptime|pingdom|statuscake|betteruptime|render/|kube-probe)", re.I)

# Our own test harnesses, self-identified by UA. Narrow on purpose: these are
# names WE ship, not generic tooling (generic tooling is handled separately).
AG_TEST_UA_RE = re.compile(
    r"(colddiscoveryharness|pilot-?a-audit|guild-ops-check|agentguild-selftest)",
    re.I)


def caller_class(event: Mapping[str, Any], *,
                 member: bool = False, verified: bool = False,
                 operator: bool = False) -> str:
    """Classify WHO produced `event` into one of CALLER_CLASSES.

    `member`   — the caller presented a valid registered api key.
    `verified` — the member has completed the proving rung (key_proof).
    `operator` — the call carried the admin token.
    The store decides those three; this function owns everything UA-derived.
    """
    if operator:
        return "OPERATOR"
    if event.get("fp", event.get("first_party")):
        return "AG_INTERNAL"
    ua = (event.get("ua", event.get("user_agent")) or "").strip()
    if _is_known_first_party_incident(event) or AG_TEST_UA_RE.search(ua):
        return "AG_TEST"
    if CRAWLER_UA_RE.search(ua):
        return "REGISTRY_CRAWLER"
    if member and verified:
        return "EXTERNAL_VERIFIED"
    if member:
        return "EXTERNAL_MEMBER"
    return "EXTERNAL_UNKNOWN"


def may_count_as_external_growth(cls: str) -> bool:
    """The single gate for external-growth metrics: crawlers and our own
    traffic can never inflate them, by type rather than by policy."""
    return cls in ("EXTERNAL_UNKNOWN", "EXTERNAL_VERIFIED", "EXTERNAL_MEMBER")


# ---------------------------------------------------------------------------
# Per-caller actor attribution for anonymous A2A traffic.
#
# The bug this fixes (2026-07-08): every inbound A2A message recorded its event
# against the literal actor key "a2a". That collapsed EVERY anonymous caller —
# a real external decider, an uptime monitor, a directory crawler — into one
# bucket, so `genuine_external_engaged_detected` could not tell them apart. We
# now derive a stable, granular key per caller from the strongest identity
# signal available, in priority order:
#   1. an explicit agent/client id header (or an agent_id named in the message)
#   2. an API key / bearer token — FINGERPRINTED, never stored raw (a secret
#      must never land in the event log, and a header must never be usable to
#      impersonate a real billing key)
#   3. a network + user-agent fingerprint from the source headers / peer IP
#   4. a stable anonymous fallback — never plain "a2a"
# Every derived key is namespaced under "a2a:" so it can NEVER collide with a
# real billing key (ak_/sk_) and can never be spoofed into first-party. IPs and
# tokens are hashed, so the event log holds no raw addresses or secrets.
# ---------------------------------------------------------------------------

_AGENT_ID_RE = re.compile(r"\bagent_[0-9a-f]{8,16}\b")

# Headers a caller may use to self-identify, most-authoritative first.
_ID_HEADERS = ("x-agent-id", "x-client-id", "x-caller-id")
_TOKEN_HEADERS = ("x-api-key", "authorization")
_IP_HEADERS = ("cf-connecting-ip", "x-real-ip", "x-forwarded-for")


def _fp(s: str, n: int = 16) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:n]


def derive_a2a_actor(headers: Mapping[str, str], client_host: str = "",
                     text: str = "") -> str:
    """Return a stable, granular actor key for an anonymous A2A caller.

    Replaces the collapsed literal "a2a". Deterministic for the same caller
    signal, so repeat calls from one monitor share a bucket while two distinct
    callers do not. Result is always namespaced "a2a:<scheme>:<fingerprint>".
    """
    def h(name: str) -> str:
        return (headers.get(name) or "").strip()

    # 1. explicit self-declared identity (header wins; else an agent_id in body)
    for hdr in _ID_HEADERS:
        v = h(hdr)
        if v:
            return "a2a:aid:" + _fp(f"{hdr}={v.lower()}")
    m = _AGENT_ID_RE.search(text or "")
    if m:
        return "a2a:aid:" + _fp(f"msg={m.group(0)}")

    # 2. API key / bearer token — fingerprinted, never raw.
    for hdr in _TOKEN_HEADERS:
        v = h(hdr)
        if v:
            return "a2a:key:" + _fp(v)

    # 3. network + user-agent fingerprint.
    ip = ""
    for hdr in _IP_HEADERS:
        v = h(hdr)
        if v:
            ip = v.split(",")[0].strip()   # first hop of x-forwarded-for
            break
    if not ip:
        ip = (client_host or "").strip()
    ua = h("user-agent")
    if ua or ip:
        return "a2a:net:" + _fp(f"{ua}|{ip}")

    # 4. stable anonymous fallback — NOT plain "a2a".
    return "a2a:anon:" + _fp("unattributable")


# ---------------------------------------------------------------------------
# Honest engagement classification.
#
# `genuine_external` counts any framework/MCP/agent UA, so it must NOT be read
# as "a real agent decided to use us". A caller is ENGAGED only if it took a
# deciding action of its own. Two traps this guards against:
#   * guild-side surfacing — every inbound A2A message unconditionally emits a
#     `prove_surfaced` event (and, on intent, `*_howto_served`) against the
#     caller's key. Those are OUR replies, not the caller's action. The old
#     "engaged = not a bare probe" rule miscounted them as engagement, so a
#     pure poller always tripped `genuine_external_engaged_detected` (the
#     2026-07-08 muddiness). They are neither probe nor engagement.
#   * bare probes — a liveness/`ping` A2A message carrying no capability, no
#     payment, no intent. Genuine traffic, but not a decision.
# ---------------------------------------------------------------------------

# Guild-side responses recorded against the caller's key — never engagement.
GUILD_SURFACING_TYPES = {
    "prove_surfaced", "prove_howto_served", "endpoint_declare_howto_served",
}

# Caller actions that, on their own, are strong evidence of a deciding agent
# (not merely a capability-shaped probe an automated monitor could emit).
STRONG_DECIDING_TYPES = {
    "register", "key_proof", "prove_started", "endpoint_declared",
    "config_change", "delegation", "attestation_given",
    "attestation_received", "first_receipt", "demand_watch",
}


def is_bare_probe(event: Mapping[str, Any]) -> bool:
    """A liveness/handshake A2A probe: an a2a_message query with no capability,
    no payment, and no non-probe intent stamped by the endpoint."""
    return bool(
        event.get("type") == "query"
        and event.get("endpoint") == "a2a_message"
        and not event.get("paid")
        and not event.get("capability")
        and event.get("caller_kind") in (None, "probe"))


def engagement_kind(event: Mapping[str, Any]) -> str:
    """Classify a *genuine-external* event: 'guild_surfacing' | 'probe' |
    'deciding'. Only 'deciding' events count toward engagement."""
    if event.get("type") in GUILD_SURFACING_TYPES:
        return "guild_surfacing"
    if is_bare_probe(event):
        return "probe"
    return "deciding"


def is_strong_deciding(event: Mapping[str, Any]) -> bool:
    """A deciding event that is strong on its own — a registration, proof,
    declaration, delegation, attestation, or a paid read. A single capability-
    shaped A2A ask is deciding but NOT strong (a monitor could emit it), so
    strength additionally accrues from repetition, handled by the caller."""
    if engagement_kind(event) != "deciding":
        return False
    return bool(event.get("type") in STRONG_DECIDING_TYPES or event.get("paid"))
