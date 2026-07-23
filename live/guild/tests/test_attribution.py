"""Honest 'genuine external agent' detection — we must not fool ourselves.

The failure this locks against: a `Python-urllib/3.10` call from our own
verification tooling being counted as the 'first genuine external agent'. Bare
tooling (curl/urllib/empty UA) with an anonymous actor is NOT genuine external;
only an attributable, non-ours identity is.
"""
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from app.store import Store  # noqa: E402
from app.attribution import (is_genuine_external, attribution_class,  # noqa: E402
                             caller_class)


def test_our_own_tooling_traffic_is_not_genuine_external():
    for ua in ("Python-urllib/3.10", "curl/8.4.0", "python-requests/2.31", "",
               "Wget/1.21", "mcp/remote", "mcp:fastmcp/1.0", "mcp:agent-guild/1"):
        e = {"fp": False, "ua": ua, "key": "anon"}
        assert is_genuine_external(e) is False, ua


def test_bare_registered_key_with_no_agent_ua_is_not_genuine():
    # our own pre-tagging seed/test accounts look exactly like this: a real ak_/sk_
    # key with an empty UA. A key ALONE is not proof of a third party.
    assert is_genuine_external({"fp": False, "ua": "", "key": "ak_legacyours"}) is False
    assert is_genuine_external({"fp": False, "ua": "", "key": "sk_legacyours"}) is False


def test_self_identifying_agents_are_genuine_external():
    # an MCP client that named itself and isn't ours
    assert is_genuine_external({"fp": False, "ua": "mcp:acme-orchestrator/2.1", "key": "mcp"}) is True
    # a recognised agent-framework UA
    assert is_genuine_external({"fp": False, "ua": "langchain/0.2 python-httpx/0.27", "key": "anon"}) is True
    # first-party is never genuine external, whatever the UA
    assert is_genuine_external({"fp": True, "ua": "mcp:acme/1", "key": "ak_x"}) is False


def test_attribution_labels():
    assert attribution_class({"fp": True, "ua": "x", "key": "ak_x"}) == "first_party"
    assert attribution_class({"fp": False, "ua": "mcp:acme/1", "key": "mcp"}) == "genuine_external"
    assert attribution_class({"fp": False, "ua": "mcp/remote", "key": "mcp"}) == "unattributable_mcp"
    assert attribution_class({"fp": False, "ua": "Python-urllib/3.10", "key": "anon"}) == "tooling_or_ours"


def test_instrumentation_exposes_honest_signal():
    s = Store(path="")
    # our own verification traffic: a urllib best_agent hit, anon
    s.record_event(None, "query", ua="Python-urllib/3.10", endpoint="best_agent", paid=False)
    instr = s.instrumentation()
    # 'external' counts it (not first-party)...
    assert instr["external"]["unique_agents"] == 1
    # ...but the HONEST signal does NOT — no real agent has arrived.
    assert instr["genuine_external_detected"] is False
    assert instr["genuine_external"]["unique_agents"] == 0
    assert instr["first_genuine_external_at"] is None

    # now a genuinely external, attributable agent (a real registered key) calls
    acct = s.create_account(first_party=False)          # ak_...
    s.record_event(acct["key"], "query", ua="langchain/0.2", endpoint="best_agent", paid=False)
    instr2 = s.instrumentation()
    assert instr2["genuine_external_detected"] is True
    assert instr2["genuine_external"]["unique_agents"] == 1
    assert acct["key"] in instr2["genuine_external_actors"]


def test_bare_probe_poller_excluded_from_engaged_signal():
    """A framework-UA caller that only ever sends bare A2A probes (no capability,
    no registration, no proof) is genuine_external traffic but NOT engagement.
    Retention must be read off genuine_external_engaged, not the raw count, so an
    uptime monitor / directory crawler can't masquerade as a returning agent.
    (2026-07-08: a single a2a:python-httpx poller produced 74/81 genuine events.)"""
    s = Store(path="")
    # A poller: many bare a2a_message probes, framework UA, anonymous, no capability.
    for _ in range(20):
        s.record_event("a2a", "query", ua="a2a:python-httpx/0.28.1",
                       endpoint="a2a_message", text="ping")
    instr = s.instrumentation()
    # It IS genuine external traffic (honesty — we don't hide it)...
    assert instr["genuine_external_detected"] is True
    assert instr["genuine_external_events"] == 20
    # ...but it is NOT engagement: zero deciding actors, poller isolated.
    assert instr["genuine_external_engaged_detected"] is False
    assert instr["genuine_external_engaged"]["unique_agents"] == 0
    assert "a2a" in instr["genuine_external_probe_only_actors"]
    assert instr["genuine_external_probe_only_events"] == 20

    # Now a genuinely deciding external agent: a capability ask over a2a.
    s.record_event("a2a", "query", ua="a2a:langchain/0.2",
                   endpoint="a2a_message", capability="fact-check")
    instr2 = s.instrumentation()
    assert instr2["genuine_external_engaged_detected"] is True
    assert "a2a" in instr2["genuine_external_engaged"]["actors"]


def test_recent_feed_labels_each_event():
    s = Store(path="")
    s.record_event(None, "query", ua="Python-urllib/3.10", endpoint="best_agent")
    ev = s.recent_events(limit=5, external_only=True)[0]
    assert ev["genuine_external"] is False
    assert ev["attribution"] == "tooling_or_ours"


def test_known_first_party_incident_excluded_inside_window_only():
    # The 2026-07-02 incident: our own crewAI-tool verification call that
    # omitted the X-Guild-Source header. Inside the window: NOT genuine.
    incident = {"fp": False, "key": "anon",
                "ua": "crewai-tools-agentguild/1.0",
                "at": "2026-07-02T08:16:07.119496+00:00"}
    assert is_genuine_external(incident) is False
    assert attribution_class(incident) == "first_party_incident"

    # The SAME UA outside the window (a real crewAI-tools user) still counts.
    real_user = dict(incident, at="2026-07-03T12:00:00+00:00")
    assert is_genuine_external(real_user) is True
    assert attribution_class(real_user) == "genuine_external"

    # And the public feed shape is honoured too.
    feed_shape = {"first_party": False, "actor": "anon",
                  "user_agent": "crewai-tools-agentguild/1.0",
                  "at": "2026-07-02T08:16:07.119496+00:00"}
    assert is_genuine_external(feed_shape) is False


def test_mainnet_canary_incident_read_time_correction():
    """The 2026-07-21 mainnet canary paid twice through /check (settlement
    07:26:33Z + idempotent re-serve, evidence 07:30:25Z) BEFORE first-party
    self-tagging shipped, under httpx's default framework UA. Read-time
    correction: inside the 20-minute window, python-httpx is a known
    first-party incident — outside it, python-httpx keeps counting."""
    settle = {"fp": False, "key": "anon", "ua": "python-httpx/0.27.2",
              "at": "2026-07-21T07:26:40.500000+00:00",
              "type": "query", "paid": True, "rail": "x402"}
    reserve = dict(settle, at="2026-07-21T07:30:10.000000+00:00")
    for e in (settle, reserve):
        assert is_genuine_external(e) is False
        assert attribution_class(e) == "first_party_incident"
        assert caller_class(e) == "AG_TEST"

    # python-httpx OUTSIDE the window is NOT demoted — a real external
    # agent on httpx still counts, before and after the incident.
    before = dict(settle, at="2026-07-21T07:19:59+00:00")
    after = dict(settle, at="2026-07-21T07:40:01+00:00")
    next_day = dict(settle, at="2026-07-22T09:00:00+00:00")
    for e in (before, after, next_day):
        assert is_genuine_external(e) is True, e["at"]
        assert attribution_class(e) == "genuine_external"

    # a DIFFERENT framework UA inside the window is untouched.
    other = dict(settle, ua="langchain/0.2.1")
    assert is_genuine_external(other) is True

    # version-suffix variations of the canary UA are covered; lookalike
    # prefixes are not.
    assert is_genuine_external(dict(settle, ua="python-httpx")) is False
    assert is_genuine_external(dict(settle, ua="python-httpxish/1.0")) is True


def test_canary_incident_moves_funnel_paid_decision_to_first_party():
    """End-to-end through the store: a paid x402 query event recorded inside
    the canary window lands in the funnel's paid_decision FIRST_PARTY bucket
    (and out of genuine_external paid_query), without touching the
    append-only event history."""
    s = Store(path="")
    s.record_event(None, "query", ua="python-httpx/0.27.2", paid=True,
                   rail="x402", endpoint="check.get")
    # pin the recorded timestamps into the incident window (read-time
    # classification means ONLY the read changes, never the stored event)
    s.events[-1]["at"] = "2026-07-21T07:26:40+00:00"
    s.record_event(None, "query", ua="python-httpx/0.27.2", paid=True,
                   rail="x402", endpoint="check.get")
    s.events[-1]["at"] = "2026-07-21T07:30:10+00:00"
    # a genuinely external paid read outside the window, for contrast
    s.record_event(None, "query", ua="langchain/0.2.1", paid=True,
                   rail="credits_sandbox", endpoint="check.get")
    s.events[-1]["at"] = "2026-07-22T09:00:00+00:00"

    funnel = s.conversion_funnel()
    paid = next(st for st in funnel["stages"]
                if st["stage"] == "paid_decision")
    assert paid["breakdown"]["first_party"] == 2
    assert paid["count"] == paid["breakdown"]["external"] == 1

    instr = s.instrumentation()
    assert instr["genuine_external"]["paid_query"] == 1
    # the stored events themselves are untouched (append-only history)
    assert [e["ua"] for e in s.events[:2]] == ["python-httpx/0.27.2"] * 2
