"""Honest recovery of genuine historical demand (machine-attribution pass).

The live event log contains PRE-RECORDER A2A capability asks (e.g.
`korean-legal`, `fact-check`) recorded as `query` events with a capability
— from before the transport-neutral demand recorder existed. Recover that
demand at READ time, without rewriting history:

  * derive legacy demand ONLY from explicit capability-ask query events
    with a capability and acceptable attribution;
  * dedupe across derived queries and canonical capability_demand events
    by actor, capability and window;
  * label provenance `legacy_derived_heuristic` — NEVER cryptographically
    verified external demand;
  * caller-proof-backed asks are `verified_machine_demand`, exposed
    separately;
  * the read-only scout consumes qualified heuristic demand.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app import callerproof, crypto, demand
from app.state import store

EXT_UA = "external-agent-framework/2.0 (crewai)"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    yield


def _cap():
    return "legacy-" + uuid.uuid4().hex[:8]


def _legacy_ask(cap, actor="a2a:net:legacy1", ua="a2a:" + EXT_UA):
    """A pre-recorder A2A capability ask, exactly as the live log holds
    them: a query event with endpoint=a2a_message and a capability."""
    store.record_event(actor, "query", ua=ua, endpoint="a2a_message",
                       text=f"check: {cap}", caller_kind="capability_ask",
                       capability=cap)


def _row(cap):
    return next((r for r in store.demand_feed_entries()
                 if r["capability"] == cap), None)


def test_legacy_asks_surface_as_heuristic_demand_without_rewriting_history():
    cap = _cap()
    _legacy_ask(cap)
    before = [e for e in store.events if e.get("capability") == cap]
    row = _row(cap)
    assert row is not None, "pre-recorder capability asks must be recovered"
    assert row["heuristic_lookups"] >= 1
    assert row["verified_lookups"] == 0
    assert "legacy_derived_heuristic" in row["provenance"], (
        "recovered demand must be labelled, never claimed as verified")
    after = [e for e in store.events if e.get("capability") == cap]
    assert after == before, "history must not be rewritten"


def test_legacy_derivation_requires_capability_and_attribution():
    # a greeting/probe with no capability derives nothing
    store.record_event("a2a:net:x", "query", ua="a2a:" + EXT_UA,
                       endpoint="a2a_message", text="hello",
                       caller_kind="probe", capability=None)
    # a first-party legacy ask derives nothing
    cap_fp = _cap()
    store.record_event("ops", "query", ua="a2a:" + EXT_UA,
                       endpoint="a2a_message", text=f"check: {cap_fp}",
                       caller_kind="capability_ask", capability=cap_fp,
                       fp=True)
    assert _row(cap_fp) is None
    # a crawler legacy ask derives nothing
    cap_crawl = _cap()
    store.record_event("a2a:net:y", "query", ua="Glama-Bot/2.0 (+crawler)",
                       endpoint="a2a_message", text=f"check: {cap_crawl}",
                       caller_kind="capability_ask", capability=cap_crawl)
    assert _row(cap_crawl) is None


def test_dedupe_across_derived_and_canonical_events():
    """The same actor's ask recorded BOTH as a legacy query event and as a
    canonical capability_demand (the transition window) counts once."""
    cap = _cap()
    actor = "a2a:net:dedup1"
    _legacy_ask(cap, actor=actor)
    demand.record_demand(cap, transport="a2a", actor=actor,
                         ua="a2a:" + EXT_UA)
    row = _row(cap)
    assert row is not None
    assert row["heuristic_lookups"] == 1, (
        "derived + canonical from the same actor/capability/window must "
        f"count once, got {row['heuristic_lookups']}")


def test_verified_machine_demand_is_separate_from_heuristic():
    from app.main import app
    import base64
    import json as _json
    cap = _cap()
    priv, pub = crypto.generate_keypair()
    did = crypto.did_from_public_key(pub)
    resource = f"/check?capability={cap}"
    env = callerproof.create_proof(priv, did, method="GET",
                                   resource=resource, body=b"")
    with TestClient(app) as client:
        client.get(resource, headers={
            "User-Agent": EXT_UA,
            callerproof.HTTP_HEADER: base64.b64encode(
                _json.dumps(env).encode()).decode()})
        client.get(f"/check?capability={cap}",
                   headers={"User-Agent": EXT_UA})     # heuristic ask
        row = _row(cap)
        assert row["verified_lookups"] == 1
        assert row["heuristic_lookups"] >= 1
        assert "verified_machine_demand" in row["provenance"]
        feed = client.get("/demand/feed").json()
        entry = next(e for e in feed["entries"] if e["capability"] == cap)
        assert entry["verified_lookups"] == 1
        assert entry["heuristic_lookups"] >= 1
        assert "verified" in feed["entry_fields"]["verified_lookups"].lower()
        assert "heuristic" in str(
            feed["entry_fields"]["heuristic_lookups"]).lower()


def test_scout_consumes_qualified_heuristic_demand():
    from app.swarm import scout
    cap = _cap()
    _legacy_ask(cap)
    seen = []

    def adapter(capability, fetch):
        seen.append(capability)
        return []

    scout.run_scout(store, fetch=lambda u, **k: (None, "no"),
                    adapters={"x": adapter})
    assert cap in seen, (
        "the read-only scout must respond to qualified legacy heuristic "
        "demand")
