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
  * a recognised independent agent-framework user-agent
    (httpx/langchain/openai/... ) that is not bare tooling.

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
    r"(httpx|aiohttp|langchain|langgraph|openai|anthropic|claude|llamaindex|"
    r"crewai|autogen|"
    r"semantic-kernel|node-fetch|undici|axios|okhttp|go-http-client|reqwest|"
    r"cursor|cline|continue|windsurf|cody|dify|n8n|flowise)", re.I)

# Agent Guild publishes this User-Agent in its own installable skill and tells
# downstream runtimes to preserve it. It proves propagation of our client
# instructions, not an independent counterparty: the same identifier can be
# emitted by our own worker or copied by any caller. Keep it distinct from
# AG_TEST (a real third party may use the skill) and from EXTERNAL_* (the UA
# alone is never enough to claim external demand or revenue).
PROPAGATION_UA_RE = re.compile(r"\bagentguild-skill(?:/|\b)", re.I)

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
    # live/scripts/live_contract_probe.py sends
    # clientInfo.name = "guild-live-conformance", so `_mcp_client` sees
    # `mcp:guild-live-conformance` — a NAMED client, which
    # `is_genuine_external` accepts as third-party unless we declare it as
    # ours. It was not declared, so our own release-gate probe registered as a
    # qualified external agent: on the 2.0.3 deployment it was the ONLY
    # remaining qualified actor in /funnel/paid, under paid_offer:mcp_tool.
    # tests/test_owned_probe_clients.py pins this to the probe's actual
    # clientInfo name so renaming the probe fails loudly instead of silently
    # re-opening the hole.
    "guild-live-conformance",
}

# Guild-operated agents that were created before durable first-party tagging
# was enforced.  These exact production identities are public and auditable:
# four TanukiTextStats registrations declare
# ``operator=agent-guild (first-party demo supply)`` and the fifth is the
# operator-owned Codex Autonomous Worker.  Their old account/event rows were
# written with ``first_party=false`` and therefore used to qualify as external
# verified members at read time.  Keep this a narrow exact-ID set: it may only
# demote known-owned traffic and must never infer ownership from a name/domain.
KNOWN_GUILD_OPERATED_AGENT_IDS = frozenset({
    "agent_143203b6a77b",
    "agent_5dd9bd352a22",
    "agent_87bcabedf2c0",
    "agent_c7d2e902dc50",
    "agent_f75dd36ac192",
})

# Known first-party incidents: OUR OWN traffic that slipped past first-party
# tagging (e.g. a maintainer test that forgot the X-Guild-Source header) and
# would otherwise read as genuine external. Each entry is deliberately narrow —
# an exact UA within a bounded time window — and documents why, so this can
# never silently hide a real agent. The same UA OUTSIDE the window still counts.
KNOWN_FIRST_PARTY_INCIDENTS: list[dict[str, str]] = [
    {
        "actor_alias_sha256": (
            "f7ea1d14b9d84c12b41afdb0cf0e872726583f1edc06ed58b367af3376b484a4"
        ),
        "reason": "Guild-operated Hugging Face hf-discover v1.3.7 "
                  "interoperability audit on 2026-08-16. The official "
                  "discover/0.1 client fetched the live ARD manifest during "
                  "a reference-client compatibility test and the resulting "
                  "HTTP registration was incorrectly eligible as T3. This is "
                  "the exact privacy-safe actor alias committed by the signed "
                  "census, not a UA, network, name, or time-range inference.",
    },
    {
        "actor_alias_sha256": (
            "daaccb419bcecff13b71ed46af0e57d833a9d4884a89e12887c8d07cdd050298"
        ),
        "reason": "Second exact actor alias produced by the same Guild-operated "
                  "Hugging Face hf-discover v1.3.7 interoperability audit on "
                  "2026-08-16, recorded on the paid_offer:manifest surface. "
                  "It is exact-pinned from the signed public evidence and may "
                  "only demote that known-owned actor.",
    },
    {
        "ua_re": r"^(?:a2a:)?langchain/0\.2\.1$",
        "from": "2026-08-14T03:11:00+00:00",
        "to": "2026-08-14T03:16:00+00:00",
        "reason": "Guild-operated official-client interoperability audit. "
                  "The audit intentionally exercised the HTTP and A2A buyer "
                  "paths against production with LangChain-shaped client "
                  "identity, but omitted authenticated first-party tagging. "
                  "The append-only feed proves ten challenge events from "
                  "eight distinct actors between 03:11:16Z and 03:15:01Z; "
                  "those calls moved /commercial "
                  "from its 2-actor/22-event/4-offer baseline to "
                  "10 actors/32 events/14 offers while revenue and paid "
                  "completions stayed zero. This bounded read-time correction "
                  "demotes only those known audit calls; the same official "
                  "client UA outside the window remains external.",
    },
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
    {
        # `ua_re` (anchored) instead of an exact `ua` because the recorded UA
        # carries the client library's version suffix; the 20-minute window is
        # what keeps this narrow — python-httpx OUTSIDE it still counts as a
        # framework UA, exactly as before. This entry demotes the two known
        # canary events and nothing else.
        "ua_re": r"^python-httpx(/|$)",
        "from": "2026-07-21T07:20:00+00:00",
        "to": "2026-07-21T07:40:00+00:00",
        "reason": "First-party mainnet canary settlement (tx 0x1052fa51aa1412"
                  "119581194acc1011c51786a59538f46bb5f9d593f1ad16d802, Base "
                  "block 0x2ea6123 at 2026-07-21T07:26:33Z) and its idempotent "
                  "crash-recovery re-serve (evidence written 07:30:25Z). The "
                  "canary predates first-party self-tagging (shipped in "
                  "b606ae5 the same morning), so its paid /check reads carried "
                  "no first-party header and — via the httpx framework UA — "
                  "inflated genuine_external and external paid_decision "
                  "telemetry. Guild-operated spend is NEVER external.",
    },
]


# Exact privacy-safe aliases for external registry scanners whose generic HTTP
# libraries otherwise look like agent frameworks. These three actors appeared
# in one 21-second burst while the skills.sh listing was being reindexed on
# 2026-08-16: one walked the A2A/Agent Skills/Guild manifests, one fetched the
# agent card, and one fetched OpenAPI. They are the scanner pipeline that
# produced the listing's security audit, not autonomous agents. Exact aliases
# avoid demoting any unrelated use of undici/node-fetch/axios/httpx.
KNOWN_REGISTRY_CRAWLER_ACTOR_ALIASES = frozenset({
    "29e06ce658cd7a3b1144029ab719435b08435849e71d3bd661a9d96d64bb418e",
    "e13d168323c33a63e335c28d2e0bb1fa90a927a9dbc5715a7fd49ca53115ba14",
    "f6ddda7ffa318e3bf5e21f9ecb0b02c48386f4b20b49da78c60f90c06bdd4213",
})


def _census_actor_alias_sha256(event: Mapping[str, Any]) -> str:
    actor = str(event.get("key", event.get("actor")) or "")
    if not actor or actor == "anon":
        return ""
    return hashlib.sha256(
        ("agent-guild/census/v1|" + actor).encode("utf-8")
    ).hexdigest()


def _is_known_registry_crawler_actor(event: Mapping[str, Any]) -> bool:
    return (_census_actor_alias_sha256(event)
            in KNOWN_REGISTRY_CRAWLER_ACTOR_ALIASES)


def _is_known_first_party_incident(event: dict[str, Any]) -> bool:
    ua = (event.get("ua", event.get("user_agent")) or "").strip()
    at = event.get("at") or ""
    actor_alias = _census_actor_alias_sha256(event)
    for inc in KNOWN_FIRST_PARTY_INCIDENTS:
        if (actor_alias and inc.get("actor_alias_sha256") == actor_alias):
            return True
        if "from" not in inc or "to" not in inc:
            continue
        if not (inc["from"] <= at <= inc["to"]):
            continue
        if "ua" in inc and ua == inc["ua"]:
            return True
        if "ua_re" in inc and re.match(inc["ua_re"], ua):
            return True
    return False


def _mcp_client(ua: str) -> str | None:
    if not ua.startswith("mcp:"):
        return None
    return ua[4:].split("/", 1)[0].strip().lower() or None


def is_genuine_external(event: dict[str, Any]) -> bool:
    """True iff `event` is attributable to an agent we do not operate.

    Accepts either the internal event shape (keys `fp`, `ua`, `key`) or the public
    feed shape (keys `first_party`, `user_agent`, `actor`).

    CENTRAL ANALYTICS INVARIANT (2026-07-10): an event whose caller class is
    AG_INTERNAL, AG_TEST, OPERATOR or REGISTRY_CRAWLER can NEVER be genuine
    external. This is the single gate every genuine_external metric flows
    through (store.instrumentation filters on this function), so the invariant
    holds for current metrics, historical aggregation (classification is
    read-time) and any dashboard built on them. Guarded by
    tests/test_analytics_invariant.py."""
    cls = caller_class(event)
    if not may_count_as_external_growth(cls):
        return False
    ua = (event.get("ua", event.get("user_agent")) or "").strip()

    # This AG-authored identity may appear as either an HTTP User-Agent or an
    # MCP clientInfo name (`mcp:agentguild-skill/...`). Guard it before the MCP
    # short-circuit: neither transport proves an independent counterparty.
    if PROPAGATION_UA_RE.search(ua):
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
    if event.get("op"):
        return "operator"             # admin-token action, auditable
    if AG_TEST_UA_RE.search(ua):
        return "ag_test"              # our own self-identified harnesses
    if CRAWLER_UA_RE.search(ua):
        return "registry_crawler"     # indexes manifests, never an agent
    if PROPAGATION_UA_RE.search(ua):
        return "propagation_client"   # AG-authored identifier, no external proof
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
    "PROPAGATION_CLIENT", "EXTERNAL_UNKNOWN", "EXTERNAL_VERIFIED",
    "EXTERNAL_MEMBER", "OPERATOR",
)

# Registry / search-engine / uptime crawlers: they index manifests, they do
# not perform tasks. GolemreachTrustBot's public contract is unusually explicit:
# one card GET plus MCP initialize/tools-list, and it never calls tools. Match its
# exact product token rather than the generic Golemreach domain so real game agents
# remain eligible. Matched anywhere in the UA, case-insensitive.
CRAWLER_UA_RE = re.compile(
    r"(glama|smithery|modelcontextprotocol|a2aregistry|golemreachtrustbot|"
    r"crawler|spider|"
    r"bingbot|googlebot|gptbot|claudebot|ccbot|censys|shodan|"
    r"uptime|pingdom|statuscake|betteruptime|render/|kube-probe)", re.I)

# Our own test harnesses, self-identified by UA. Narrow on purpose: these are
# names WE ship, not generic tooling (generic tooling is handled separately).
AG_TEST_UA_RE = re.compile(
    r"(colddiscoveryharness|pilot-?a-audit|guild-ops-check|agentguild-selftest|"
    # the MCP canary (mcp:guild-canary/1 or a bare guild-canary token fallback):
    # it is first-party by construction, so match its UA server-side too — it must
    # never read as genuine_external even if its runtime lacks the first-party
    # token (found leaking 2026-07-11).
    # `guild-live-conformance` is the LIVE CONTRACT PROBE
    # (live/scripts/live_contract_probe.py). It ships with the repo, runs in
    # the release gate, and identifies itself both as a User-Agent and as an
    # MCP clientInfo.name. Listed here as an exact literal — never a wildcard —
    # so it is AG_TEST on every transport rather than reading as a stranger.
    r"guild-canary|guild-reachability-probe|guild-live-conformance)",
    re.I)


# STRUCTURAL first-party origins. An event carrying one of these was produced
# BY THIS PROCESS (the scout loop, the index observer, the swarm runner) rather
# than by an inbound caller. It is first-party by construction — there is no
# remote actor to misclassify — so it is decided BEFORE any UA or header logic.
#
# Why this exists (2026-08-01): the autonomous scout/index loops emitted
# `candidate_*`, `index_observation` and `scout_cycle_completed` events with no
# actor key and no first-party header, so `caller_class` fell all the way
# through to EXTERNAL_UNKNOWN. In one 24h window that put ~3,800 of our OWN
# events into the broad-external bucket and made 168 of the 200 most recent
# "external" events ours. Qualified metrics already excluded them by a
# secondary `attribution` field, but the raw telemetry was polluted and the
# exclusion depended on a fragile derived label rather than on the event's
# origin. UA matching cannot fix this: our own loops have no distinctive UA,
# and any string we invented could be spoofed by an outside caller to launder
# itself INTO our first-party bucket.
GUILD_INTERNAL_ORIGINS = frozenset({
    "swarm_scout",         # candidate discovery / refresh / verification
    "index_observer",      # index coverage observations
    "swarm_runner",        # cycle bookkeeping
    "ops_selfcheck",       # in-process health/self-evaluation ticks
    "experiment_engine",   # autonomous experiment bookkeeping
})


def is_guild_internal_origin(event: Mapping[str, Any]) -> bool:
    """True iff the event was produced inside this process by a named Guild
    subsystem. Structural: the origin is stamped at the emit site, never
    inferred from a header or user agent a caller controls."""
    return (event.get("origin") or "") in GUILD_INTERNAL_ORIGINS


def caller_class(event: Mapping[str, Any], *,
                 member: bool = False, verified: bool = False,
                 operator: bool = False) -> str:
    """Classify WHO produced `event` into one of CALLER_CLASSES.

    `member`   — the caller presented a valid registered api key.
    `verified` — the member has completed the proving rung (key_proof).
    `operator` — the call carried the admin token.
    The store decides those three; this function owns everything UA-derived.
    """
    # STRUCTURAL first-party: emitted by one of our own in-process subsystems.
    # Checked FIRST — before operator, headers or UA — because it is the only
    # classification that cannot be influenced by a remote caller.
    if is_guild_internal_origin(event):
        return "AG_INTERNAL"
    if operator or event.get("op"):
        return "OPERATOR"
    ua = (event.get("ua", event.get("user_agent")) or "").strip()
    fp = bool(event.get("fp", event.get("first_party")))
    # A first-party TEST caller (explicit fp_role='test', or a self-identified
    # test/verification UA, or a known first-party incident) is AG_TEST; any
    # other first-party caller is AG_INTERNAL. This is checked BEFORE the
    # generic fp->AG_INTERNAL so a first-party canary/probe is AG_TEST, not
    # AG_INTERNAL. A caller WITHOUT first-party auth can never be AG_INTERNAL.
    is_test = (event.get("fp_role") == "test"
               or _is_known_first_party_incident(event)
               or AG_TEST_UA_RE.search(ua))
    if fp and is_test:
        return "AG_TEST"
    if fp:
        return "AG_INTERNAL"
    if _is_known_registry_crawler_actor(event):
        return "REGISTRY_CRAWLER"
    # Non-first-party but self-identified AG test/verification tooling (e.g. the
    # canary before the token is set) is still AG_TEST (defense-in-depth), never
    # genuine external.
    if is_test:
        return "AG_TEST"
    if CRAWLER_UA_RE.search(ua):
        return "REGISTRY_CRAWLER"
    if PROPAGATION_UA_RE.search(ua):
        return "PROPAGATION_CLIENT"
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
# offer_served (2026-07-23) is the Guild serving the passport CTA on a
# discovery surface — OUR offer, not the caller's action; counting it as
# engagement would let any crawler with a framework UA trip the detector.
GUILD_SURFACING_TYPES = {
    "prove_surfaced", "prove_howto_served", "endpoint_declare_howto_served",
    "offer_served",
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
