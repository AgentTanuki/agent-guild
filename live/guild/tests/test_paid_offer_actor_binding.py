"""The leading metric must be able to leave zero.

Codex review, 2026-08-01, found the impression recorders structurally unable to
produce a qualified actor on two of the four surfaces:

  * `wellknown_manifest` and `llms_txt` called `_serve_paid_offer` with no
    actor, so every impression recorded actor=None even though the middleware
    had already bound `_req_actor` for the request. Both surfaces could receive
    unlimited qualified traffic and report zero qualified actors forever.
  * `guild_paid_operations` and `guild_preflight` hardcoded the literal actor
    "mcp", collapsing every MCP client in the world into ONE actor — the exact
    failure `_mcp_actor` exists to prevent.

These are TRANSPORT-LEVEL tests. Unit-testing the counter would have passed in
both broken states, which is the whole lesson from the previous six review
rounds: assert on what the system does through its real surfaces.
"""
import json
import os

os.environ["GUILD_DATA"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app import paidcatalog  # noqa: E402
from app.store import Store  # noqa: E402


def _fresh():
    """A clean store bound into the app, so counts come only from this test."""
    s = Store(path="")
    real = main.store
    main.store = s
    return s, real


def _impressions(store):
    return [e for e in store.events if e.get("type") == "paid_offer_served"]


# --------------------------------------------------------------------------
# HTTP surfaces
# --------------------------------------------------------------------------
def test_two_distinct_http_actors_produce_two_qualified_actors():
    s, real = _fresh()
    try:
        c = TestClient(main.app)
        c.get("/.well-known/agent-guild.json", headers={"user-agent": "langchain/0.2.1"})
        c.get("/llms.txt", headers={"user-agent": "crewai/0.5.0"})
        f = s.paid_offer_funnel()
        assert f["qualified_distinct_actors"] == 2, (
            f"expected 2 distinct actors, got {f['qualified_distinct_actors']} "
            f"from actors={[e.get('key') for e in _impressions(s)]}")
        assert f["measurable"] is True
        # and both surfaces are individually attributed
        assert set(f["by_source"]) == {"paid_offer:manifest",
                                       "paid_offer:llms_txt"}
        for src in f["by_source"].values():
            assert src["qualified_distinct_actors"] == 1
    finally:
        main.store = real


def test_manifest_and_llms_txt_bind_a_real_actor_not_none():
    """The specific regression: neither surface may record actor=None."""
    s, real = _fresh()
    try:
        c = TestClient(main.app)
        c.get("/.well-known/agent-guild.json", headers={"user-agent": "langchain/0.2.1"})
        c.get("/llms.txt", headers={"user-agent": "langchain/0.2.1"})
        evs = _impressions(s)
        assert evs, "no impressions recorded at all"
        for e in evs:
            assert e["key"] not in (None, "", "anon"), (
                f"{e.get('source')} recorded an unusable actor {e['key']!r}")
            assert e["key"].startswith("http:")
        # same client on both surfaces = ONE actor, not two
        assert s.paid_offer_funnel()["qualified_distinct_actors"] == 1
    finally:
        main.store = real


def test_repeat_visits_are_reach_not_extra_qualified_actors():
    s, real = _fresh()
    try:
        c = TestClient(main.app)
        for _ in range(5):
            c.get("/llms.txt", headers={"user-agent": "langchain/0.2.1"})
        f = s.paid_offer_funnel()
        assert f["qualified_distinct_actors"] == 1
        assert f["raw_impressions"] == 5 * len(paidcatalog.operations())
    finally:
        main.store = real


def test_crawler_traffic_is_excluded_from_the_denominator():
    s, real = _fresh()
    try:
        c = TestClient(main.app)
        c.get("/llms.txt", headers={"user-agent": "Glama/1.0 crawler"})
        c.get("/llms.txt", headers={"user-agent": "langchain/0.2.1"})
        f = s.paid_offer_funnel()
        assert f["qualified_distinct_actors"] == 1
        src = f["by_source"]["paid_offer:llms_txt"]
        assert src["first_party_or_tooling"] == len(paidcatalog.operations())
        assert src["qualified_impressions"] == len(paidcatalog.operations())
    finally:
        main.store = real


def test_internal_origin_traffic_is_excluded_from_the_denominator():
    s, real = _fresh()
    try:
        s.record_internal_event("paid_offer_served", "swarm_scout",
                                operation="deep_preflight",
                                source="paid_offer:llms_txt")
        TestClient(main.app).get("/llms.txt",
                                 headers={"user-agent": "langchain/0.2.1"})
        f = s.paid_offer_funnel()
        assert f["qualified_distinct_actors"] == 1
    finally:
        main.store = real


# --------------------------------------------------------------------------
# MCP surface
# --------------------------------------------------------------------------
class _Ctx:
    """Minimal MCP context carrying a clientInfo-derived user agent."""

    def __init__(self, ua):
        self._ua = ua

    def __getattr__(self, _):
        raise AttributeError


def _mcp_call(monkeypatch, store, ua):
    import app.mcp_server as mcp_server
    monkeypatch.setattr(mcp_server, "store", store, raising=False)
    monkeypatch.setattr(mcp_server, "_client_ua", lambda ctx: ua)
    fn = getattr(mcp_server.guild_paid_operations, "fn",
                 mcp_server.guild_paid_operations)
    return fn(ctx=None)


def test_distinct_mcp_clients_stay_distinct(monkeypatch):
    """Two different clientInfo strings must not collapse into one actor."""
    s = Store(path="")
    _mcp_call(monkeypatch, s, "mcp:client-alpha/2.1")
    _mcp_call(monkeypatch, s, "mcp:client-beta/0.9")
    keys = {e["key"] for e in _impressions(s)}
    assert len(keys) == 2, f"MCP clients collapsed into {keys}"
    assert "mcp" not in keys, "the hardcoded literal actor is back"
    assert s.paid_offer_funnel()["qualified_distinct_actors"] == 2


def test_bare_mcp_client_is_unlinkable_and_not_one_fabricated_actor(monkeypatch):
    """A client that advertised nothing identifying is genuinely UNKNOWABLE.
    It must not enter the denominator — neither as one actor nor as many."""
    s = Store(path="")
    _mcp_call(monkeypatch, s, "mcp/remote")
    _mcp_call(monkeypatch, s, "mcp/remote")
    evs = _impressions(s)
    assert evs and all(e["actor_distinct"] is False for e in evs)
    f = s.paid_offer_funnel()
    assert f["qualified_distinct_actors"] == 0
    assert f["measurable"] is False
    # ORDERING (2026-08-01): qualification is decided BEFORE linkability, so a
    # bare `mcp/remote` client is excluded as unrecognised tooling rather than
    # reaching the "qualified but unfollowable" bucket. The property this test
    # protects — it never becomes one fabricated qualified actor — holds either
    # way, and is asserted on the denominator above.
    assert f["by_source"]["paid_offer:mcp_tool"]["not_qualified"] == len(evs)


def test_identified_and_bare_mcp_clients_do_not_contaminate_each_other(monkeypatch):
    s = Store(path="")
    _mcp_call(monkeypatch, s, "mcp/remote")
    _mcp_call(monkeypatch, s, "mcp:client-gamma/3.0")
    f = s.paid_offer_funnel()
    assert f["qualified_distinct_actors"] == 1
    # ORDERING (2026-08-01): qualification is decided BEFORE linkability, so a
    # bare `mcp/remote` client is excluded as unrecognised rather than counted
    # as "qualified but unfollowable". Either way it never inflates the
    # denominator, which is what this test exists to protect.
    assert f["anonymous_unlinkable_impressions"] == 0
    assert f["by_source"]["paid_offer:mcp_tool"]["not_qualified"] == \
        len(paidcatalog.operations())


# --------------------------------------------------------------------------
# Registry click-through is observable; a static listing view is not
# --------------------------------------------------------------------------
def test_registry_src_produces_registry_attributed_impressions():
    """`paid_offer:registry` was declared in SOURCE_IDS with nothing able to
    emit it — the funnel would have reported a surface that could never move.
    The Registry listing now links here with ?src=paid_offer:registry."""
    s, real = _fresh()
    try:
        c = TestClient(main.app)
        c.get("/.well-known/agent-guild.json?src=paid_offer:registry",
              headers={"user-agent": "langchain/0.2.1"})
        f = s.paid_offer_funnel()
        assert "paid_offer:registry" in f["by_source"]
        assert f["by_source"]["paid_offer:registry"][
            "qualified_distinct_actors"] == 1
    finally:
        main.store = real


def test_only_the_allowlisted_src_is_honoured():
    """An open-ended `src` would let any caller mint any source id and forge
    the leading metric. Unrecognised values fall back to the manifest's own
    source; they are never recorded as what the caller claimed."""
    s, real = _fresh()
    try:
        c = TestClient(main.app)
        for bogus in ("paid_offer:mcp_tool", "totally_made_up",
                      "paid_offer:llms_txt"):
            c.get(f"/.well-known/agent-guild.json?src={bogus}",
                  headers={"user-agent": "langchain/0.2.1"})
        sources = set(s.paid_offer_funnel()["by_source"])
        assert sources == {"paid_offer:manifest"}, sources
    finally:
        main.store = real


def test_the_registry_catalog_url_returns_the_live_catalog():
    """The listing promises current price, auth, entrypoint and free
    alternative at that URL. It has to actually be there."""
    c = TestClient(main.app)
    body = c.get("/.well-known/agent-guild.json?src=paid_offer:registry"
                 ).json()["paid_operations"]
    assert body["source"] == "paid_offer:registry"
    for op in body["operations"]:
        assert op["price_usd"] and op["entrypoint"]["call"]
        assert op["free_alternative"].strip()
        assert "key_required" in op["entrypoint"]
    assert "authentication" in body


# --------------------------------------------------------------------------
# QUALIFICATION SEMANTICS (integrity correction, 2026-08-01)
# --------------------------------------------------------------------------
# The deployed 2.0.3 readback proved the denominator was wrong: every
# "qualified" actor was ours or a probe — this session's verification curls,
# a bare curl/8.7.1, `guild-live-conformance` (our own release gate),
# `agent-guild-scout` (our own scout, arriving over the network so the
# in-process origin stamp does not apply) and an A2A registry health check.
# Zero external demand, reported as measurable: true.
#
# Root cause: paid_offer_funnel gated on may_count_as_external_growth, which
# passes EXTERNAL_UNKNOWN — i.e. every bare curl/urllib/empty-UA caller.
# attribution.is_genuine_external already says exactly why that is not enough:
# such traffic is INDISTINGUISHABLE FROM OUR OWN. A stable IP+UA actor proves
# DISTINCTNESS, not external agent intent.
#
# Raw impressions are unaffected — reach is real and stays visible.

#: The exact (user_agent, actor) pairs observed on the deployed readback.
LIVE_READBACK_ACTORS = [
    ("curl/7.81.0", "http:43929a"),
    ("curl/8.7.1", "http:41b2ca"),
    ("a2a:A2A-Registry-HealthCheck/1.0", "a2a:net:0e11"),
    ("a2a:agent-guild-scout/1 (+https://agent-guild-5d5r.onrender.com)",
     "a2a:net:5e22"),
    ("a2a:guild-live-conformance", "a2a:net:c433"),
    ("guild-live-conformance", "http:07cc44"),
]


def _seed_impressions(store, pairs, source="paid_offer:registry"):
    for ua, actor in pairs:
        for op in ("deep_preflight", "evidence_bundle", "watch_cycle"):
            event = {
                "type": "paid_offer_served", "operation": op,
                "source": source, "key": actor, "ua": ua,
                "at": "2026-08-01T09:35:00+00:00"}
            store.events.append(event)
            if store.backend is not None:
                store.backend.append_event(event)
    return store


def test_the_live_readback_pattern_qualifies_nobody():
    """Replays exactly what production reported and asserts it is now zero."""
    s = _seed_impressions(Store(path=""), LIVE_READBACK_ACTORS)
    f = s.paid_offer_funnel()
    assert f["raw_impressions"] == 18, "raw reach must be preserved"
    assert f["qualified_distinct_actors"] == 0
    assert f["measurable"] is False
    src = f["by_source"]["paid_offer:registry"]
    assert src["impressions"] == 18
    assert src["qualified_impressions"] == 0
    assert src["not_qualified"] == 18


def test_exclusion_reasons_are_auditable():
    """A single opaque 'excluded' bucket is not evidence. Each exclusion
    carries the attribution class that produced it."""
    s = _seed_impressions(Store(path=""), LIVE_READBACK_ACTORS)
    reasons = s.paid_offer_funnel()[
        "by_source"]["paid_offer:registry"]["not_qualified_by_reason"]
    assert sum(reasons.values()) == 18
    assert reasons.get("tooling_or_ours", 0) == 6      # the two bare curls


def test_per_operation_and_source_raw_counts_are_preserved():
    """Raw reach must NOT be deleted or hidden by the tighter gate."""
    s = _seed_impressions(Store(path=""), LIVE_READBACK_ACTORS)
    s = _seed_impressions(s, LIVE_READBACK_ACTORS[:1], source="paid_offer:llms_txt")
    f = s.paid_offer_funnel()
    assert f["raw_impressions"] == 21
    assert set(f["by_operation"]) == {"deep_preflight", "evidence_bundle",
                                      "watch_cycle"}
    for op in f["by_operation"].values():
        assert op["impressions"] == 7
    assert f["by_source"]["paid_offer:llms_txt"]["impressions"] == 3


def test_historical_events_reclassify_at_read_time():
    """No event deletion and no log rewrite: the SAME stored events must simply
    stop reporting as qualified."""
    s = _seed_impressions(Store(path=""), LIVE_READBACK_ACTORS)
    stored = [dict(e) for e in s.events]
    f = s.paid_offer_funnel()
    assert f["qualified_distinct_actors"] == 0
    assert [dict(e) for e in s.events] == stored, "events were mutated"


@pytest.mark.parametrize("ua", [
    "curl/7.81.0", "curl/8.7.1", "wget/1.21", "python-urllib3/2.0",
    "python-requests/2.31", "", "   ", "mcp/remote",
    "ClaudeBot/1.0", "Glama/1.0 crawler", "a2aregistry-probe/1",
    "totally-unrecognised-thing/9",
])
def test_bare_tooling_crawlers_and_unknown_uas_never_qualify(ua):
    s = _seed_impressions(Store(path=""), [(ua, "actor-x")])
    assert s.paid_offer_funnel()["qualified_distinct_actors"] == 0


@pytest.mark.parametrize("ua", [
    # the ACTUAL recognised named-MCP form is `mcp:<client>/<version>` —
    # `mcp/<client>` is not a client declaration and must not be used as the
    # fixture, or this test would silently assert nothing.
    "mcp:some-third-party-client/1.2",
    "langchain/0.2.1",
    "crewai/0.5",
])
def test_named_external_clients_and_frameworks_do_qualify(ua):
    """The gate must not be so tight that nothing can ever qualify — that would
    replace a false positive with a permanently unmovable metric.

    NO SKIP ESCAPE: recognition is asserted, so if `is_genuine_external` ever
    stops recognising one of these, this fails loudly instead of quietly
    skipping."""
    from app import attribution
    ev = {"type": "paid_offer_served", "ua": ua, "key": "actor-y"}
    assert attribution.is_genuine_external(ev) is True, (
        f"{ua!r} is no longer recognised as a genuine external caller — "
        "recognition drift, not a reason to skip")
    s = _seed_impressions(Store(path=""), [(ua, "actor-y")])
    assert s.paid_offer_funnel()["qualified_distinct_actors"] == 1


def _member_credential(store):
    """Register a real agent and return (raw_secret, public_key_id).

    Hashed credentials are what production runs (`GUILD_HASH_KEYS=1` in
    render.yaml): the raw `sk_` secret is presented, the ACCOUNT is keyed by the
    public key_id, and only the raw secret resolves. The `_hashed_keys` fixture
    turns that on so these tests exercise the production shape rather than the
    legacy plaintext one."""
    agent = store.register_agent(name="paying-customer", capabilities=["x"],
                                 metadata={})
    raw = agent.get("api_key")
    kid = agent.get("key_id") or store.agents[agent["id"]].get("key_id")
    assert raw and raw.startswith("sk_"), "expected a raw sk_ secret"
    assert kid and not kid.startswith("sk_"), "expected a public key_id"
    acct = store.accounts.get(store.resolve_billing_key(raw))
    if acct is not None:
        acct.pop("first_party", None)
    return raw, kid


@pytest.fixture(autouse=False)
def _hashed_keys(monkeypatch):
    """Production credential mode. Without it the store uses legacy plaintext
    accounts, where the account key IS the raw secret — which our safety rule
    deliberately refuses to bind as an actor."""
    monkeypatch.setenv("GUILD_HASH_KEYS", "1")
    from app import credentials as _c
    if hasattr(_c, "_HASHING"):
        monkeypatch.setattr(_c, "_HASHING", None, raising=False)
    yield


def test_a_real_member_credential_qualifies_over_http_with_a_bare_ua(_hashed_keys):
    """TRANSPORT LEVEL — the member allowance was UNREACHABLE in production.

    Middleware binds `_req_actor` via `_http_demand_actor`, which used to hash
    every presented key into `http:<digest>`; no account lookup could match
    that, so EXTERNAL_MEMBER/EXTERNAL_VERIFIED never occurred on a real HTTP
    call. The previous version of this test seeded the stored account key
    directly and therefore proved nothing about the transport."""
    s, real = _fresh()
    try:
        raw, _kid = _member_credential(s)
        c = TestClient(main.app)
        r = c.get("/.well-known/agent-guild.json",
                  headers={"user-agent": "curl/7.81.0", "x-api-key": raw})
        assert r.status_code == 200
        f = s.paid_offer_funnel()
        assert f["qualified_distinct_actors"] == 1, (
            "an authenticated member with a bare curl UA must qualify; "
            f"actors seen: {[e.get('key') for e in _impressions(s)]}")
        assert f["measurable"] is True
    finally:
        main.store = real


def test_an_invalid_credential_does_not_qualify_and_is_never_echoed():
    s, real = _fresh()
    try:
        bogus = "sk_" + "b" * 40
        c = TestClient(main.app)
        c.get("/.well-known/agent-guild.json",
              headers={"user-agent": "curl/7.81.0", "x-api-key": bogus})
        f = s.paid_offer_funnel()
        assert f["qualified_distinct_actors"] == 0
        blob = json.dumps([dict(e) for e in _impressions(s)]) + json.dumps(f)
        assert bogus not in blob, "the presented credential leaked"
        assert "sk_" not in blob, "a secret-shaped value reached the journal"
    finally:
        main.store = real


def test_a_bare_public_key_id_does_not_authenticate(_hashed_keys):
    """A key_id is public — it appears in audit events. Presenting one must not
    grant member status, or the identity signal would be forgeable."""
    s, real = _fresh()
    try:
        raw, kid = _member_credential(s)
        assert kid and not kid.startswith("sk_")
        c = TestClient(main.app)
        c.get("/.well-known/agent-guild.json",
              headers={"user-agent": "curl/7.81.0", "x-api-key": kid})
        f = s.paid_offer_funnel()
        assert f["qualified_distinct_actors"] == 0, (
            "a bare public key_id authenticated as a member")
        assert kid not in json.dumps(f)
    finally:
        main.store = real


def test_a_raw_secret_never_reaches_the_events_journal(_hashed_keys):
    s, real = _fresh()
    try:
        raw, _ = _member_credential(s)
        TestClient(main.app).get(
            "/.well-known/agent-guild.json",
            headers={"user-agent": "curl/7.81.0", "x-api-key": raw})
        for e in _impressions(s):
            assert raw not in json.dumps(e)
            assert not str(e.get("key", "")).startswith("sk_")
    finally:
        main.store = real


def test_guild_internal_origin_still_never_qualifies():
    s = Store(path="")
    s.record_internal_event("paid_offer_served", "swarm_scout",
                            operation="deep_preflight",
                            source="paid_offer:registry")
    assert s.paid_offer_funnel()["qualified_distinct_actors"] == 0


def test_a_legacy_plaintext_account_never_binds_its_raw_secret_as_the_actor():
    """The `sk_` guard, exercised on the path that needs it.

    With hashing OFF (legacy plaintext accounts) the ACCOUNT KEY *is* the raw
    secret, and `sanitize_actor_key` does not rewrite it — so without the guard
    `_http_demand_actor` would bind a live credential as the event actor and it
    would land in the events journal verbatim. Verified: with hashing disabled,
    `resolve_billing_key(raw)` returns the raw `sk_` value unchanged.

    The correct behaviour is to fall back to the opaque hash: this caller loses
    member status (conservative, and legacy plaintext is being migrated out)
    but no secret is ever recorded."""
    import os as _os
    prev = _os.environ.pop("GUILD_HASH_KEYS", None)
    s, real = _fresh()
    try:
        from app import credentials as _c
        assert _c.hashing_enabled() is False, "fixture needs legacy mode"
        agent = s.register_agent(name="legacy-member", capabilities=["x"],
                                 metadata={})
        raw = agent["api_key"]
        assert s.resolve_billing_key(raw) == raw, (
            "fixture invalid: legacy accounts must key on the raw secret")

        TestClient(main.app).get(
            "/.well-known/agent-guild.json",
            headers={"user-agent": "curl/7.81.0", "x-api-key": raw})

        evs = _impressions(s)
        assert evs, "no impression recorded"
        blob = json.dumps([dict(e) for e in evs])
        assert raw not in blob, "a live raw secret reached the events journal"
        for e in evs:
            assert not str(e["key"]).startswith("sk_")
            assert str(e["key"]).startswith("http:"), (
                "expected the opaque fallback actor")
        assert s.paid_offer_funnel()["qualified_distinct_actors"] == 0
    finally:
        main.store = real
        if prev is not None:
            _os.environ["GUILD_HASH_KEYS"] = prev


def test_a_valid_ak_billing_credential_is_never_bound_as_the_actor():
    """The `ak_` case, which the first version of the credential guard missed.

    `resolve_billing_key` returns a legacy/billing `ak_` account key VERBATIM
    (the account is keyed by the raw value, not by a hash), and
    `sanitize_actor_key` only rewrites `sk_`. A guard that checked `sk_` alone
    therefore bound a LIVE, WORKING credential as the event actor — written to
    the events journal and served on the public funnel surface.

    Correct behaviour: fall back to the opaque purpose-scoped hash. This caller
    loses member status, which is the conservative trade; leaking a working
    billing key is not."""
    s, real = _fresh()
    try:
        acct = s.create_account()
        ak = acct["key"] if isinstance(acct, dict) else acct
        assert str(ak).startswith("ak_"), "fixture needs a billing account key"
        assert s.resolve_billing_key(ak) == ak, (
            "fixture invalid: an ak_ account must resolve to itself")

        r = TestClient(main.app).get(
            "/.well-known/agent-guild.json",
            headers={"user-agent": "curl/7.81.0", "x-api-key": ak})
        assert r.status_code == 200

        evs = _impressions(s)
        assert evs, "no impression recorded"
        f = s.paid_offer_funnel()
        blob = json.dumps([dict(e) for e in evs]) + json.dumps(f) + r.text
        assert ak not in blob, "a live ak_ billing credential leaked"
        assert "ak_" not in blob, "a credential-shaped value reached a surface"
        for e in evs:
            assert str(e["key"]).startswith("http:"), (
                f"expected the opaque fallback actor, got {e['key']!r}")
        assert f["qualified_distinct_actors"] == 0, (
            "a bare curl UA with a billing key must not qualify")
    finally:
        main.store = real


def test_the_credential_guard_covers_every_known_credential_prefix():
    """Cheap structural check so a new credential form cannot be added without
    the actor guard being considered."""
    assert set(main._CREDENTIAL_PREFIXES) >= {"sk_", "ak_"}
    for p in main._CREDENTIAL_PREFIXES:
        assert main._looks_like_credential(p + "abc123")
        assert main._looks_like_credential((p + "abc123").upper())
    # a public key_id (hex, no prefix) must NOT be treated as a credential,
    # or modern members would stop qualifying.
    assert not main._looks_like_credential("492a3d5040ba0e8e825843dc151d1217")
