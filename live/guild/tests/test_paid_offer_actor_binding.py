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
import os

os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
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
        c.get("/.well-known/agent-guild.json", headers={"user-agent": "buyer-one/1.0"})
        c.get("/llms.txt", headers={"user-agent": "buyer-two/1.0"})
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
        c.get("/.well-known/agent-guild.json", headers={"user-agent": "ua-a/1"})
        c.get("/llms.txt", headers={"user-agent": "ua-a/1"})
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
            c.get("/llms.txt", headers={"user-agent": "scraper/1.0"})
        f = s.paid_offer_funnel()
        assert f["qualified_distinct_actors"] == 1
        assert f["raw_impressions"] == 15          # 5 visits x 3 operations
    finally:
        main.store = real


def test_crawler_traffic_is_excluded_from_the_denominator():
    s, real = _fresh()
    try:
        c = TestClient(main.app)
        c.get("/llms.txt", headers={"user-agent": "Glama/1.0 crawler"})
        c.get("/llms.txt", headers={"user-agent": "real-buyer/1.0"})
        f = s.paid_offer_funnel()
        assert f["qualified_distinct_actors"] == 1
        src = f["by_source"]["paid_offer:llms_txt"]
        assert src["first_party_or_tooling"] == 3
        assert src["qualified_impressions"] == 3
    finally:
        main.store = real


def test_internal_origin_traffic_is_excluded_from_the_denominator():
    s, real = _fresh()
    try:
        s.record_internal_event("paid_offer_served", "swarm_scout",
                                operation="deep_preflight",
                                source="paid_offer:llms_txt")
        TestClient(main.app).get("/llms.txt",
                                 headers={"user-agent": "real-buyer/1.0"})
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
    assert f["anonymous_unlinkable_impressions"] == len(evs)


def test_identified_and_bare_mcp_clients_do_not_contaminate_each_other(monkeypatch):
    s = Store(path="")
    _mcp_call(monkeypatch, s, "mcp/remote")
    _mcp_call(monkeypatch, s, "mcp:client-gamma/3.0")
    f = s.paid_offer_funnel()
    assert f["qualified_distinct_actors"] == 1
    assert f["anonymous_unlinkable_impressions"] == 3


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
              headers={"user-agent": "registry-follower/1.0"})
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
                  headers={"user-agent": "forger/1.0"})
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
