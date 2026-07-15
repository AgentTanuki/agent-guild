"""Demand-feed honesty regressions (pre-mainnet swarm completion pass).

Four defects reproduced here, all fixed at READ time or in durable state —
never by deleting history:

  1. `genuine_lookups` must flow through the ONE central rule
     (attribution.is_genuine_external). Bare curl / empty or tooling UAs /
     AG tests / crawlers / unattributable historical traffic must never be
     published as genuine machine demand.
  2. Historical events are never deleted — they are re-interpreted when read.
  3. Demand stays in /demand/feed until the capability has a VERIFIED
     reachable supplier (verified_reachable > 0). A paper registration or a
     dead/unverified endpoint must not erase unmet demand.
  4. Dedupe state is DURABLE and keyed (actor, capability, window) — not a
     scan of the last 500 events that any event flood defeats and any
     restart forgets.
"""
import uuid
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from app import demand
from app import state as app_state
from app.state import store
from app.store import Store

PAY_TO = "0x" + "11" * 20
EXT_UA = "external-agent-framework/2.0 (crewai)"


@pytest.fixture(autouse=True)
def _enforced(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    yield


def _cap():
    return "honest-cap-" + uuid.uuid4().hex[:8]


def _row(cap):
    return next((r for r in store.demand_feed_entries()
                 if r["capability"] == cap), None)


# ---------------------------------------------------------------------------
# 1+2 — genuine_lookups through attribution.is_genuine_external, at read time
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ua", [
    "curl/8.5.0",                      # bare tooling — our own probes look like this
    "",                                # empty UA — unattributable
    "python-requests/2.31",            # bare tooling
    "Glama-Bot/2.0 (+crawler)",        # registry crawler
    "guild-ops-check/1",               # our own harness
])
def test_non_genuine_asks_never_count_as_genuine_demand(ua):
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        r = client.get(f"/check?capability={cap}",
                       headers={"User-Agent": ua} if ua else {"User-Agent": ""})
        assert r.status_code in (402, 200, 404)
    row = _row(cap)
    assert row is not None, "the ask itself must still be recorded"
    assert row["lookups"] >= 1
    assert row["genuine_lookups"] == 0, (
        f"UA {ua!r} must not read as genuine external machine demand")
    # and therefore never published on the public unmet-demand feed
    with TestClient(app) as client:
        feed = client.get("/demand/feed").json()
    assert cap not in [e["capability"] for e in feed["entries"]]


def test_genuine_framework_ask_still_counts():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        client.get(f"/check?capability={cap}", headers={"User-Agent": EXT_UA})
    row = _row(cap)
    assert row and row["genuine_lookups"] == 1


def test_historical_events_are_corrected_at_read_time_not_deleted():
    """A pre-fix event journal contains capability_demand rows recorded from
    bare tooling. The fix must re-interpret them when read — never rewrite
    or delete them."""
    cap = _cap()
    store.record_event(None, "capability_demand", ua="curl/7.88",
                       capability=cap, explicit=True, supplied=False,
                       transport="http", actor="legacy-actor",
                       demand_id=demand.demand_id_for(cap))
    before = [e for e in store.events
              if e.get("type") == "capability_demand"
              and e.get("capability") == cap]
    assert len(before) == 1
    row = _row(cap)
    assert row is not None and row["lookups"] == 1
    assert row["genuine_lookups"] == 0
    after = [e for e in store.events
             if e.get("type") == "capability_demand"
             and e.get("capability") == cap]
    assert after == before, "history must not be rewritten or deleted"


# ---------------------------------------------------------------------------
# 3 — unmet demand survives paper registrations and dead endpoints
# ---------------------------------------------------------------------------

def _ai(ip):
    import socket
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]


def test_paper_registration_does_not_erase_unmet_demand():
    from app.main import app
    from app import reachability as R
    cap = _cap()
    with TestClient(app) as client:
        client.get(f"/check?capability={cap}", headers={"User-Agent": EXT_UA})
        # a paper supplier registers: no endpoint, nothing verified
        store.register_agent(name="paper-" + cap, capabilities=[cap],
                             metadata={})
        feed = client.get("/demand/feed").json()
        entry = next((e for e in feed["entries"] if e["capability"] == cap),
                     None)
        assert entry is not None, (
            "a paper registration must NOT remove unmet demand from the feed")
        assert entry["supplied"] >= 1
        assert entry["verified_reachable"] == 0

        # a declared but UNVERIFIED endpoint still does not meet the demand
        agent = store.register_agent(name="declared-" + cap,
                                     capabilities=[cap], metadata={})
        store.set_agent_endpoint(agent["id"], "https://dead.example/a2a")
        feed = client.get("/demand/feed").json()
        assert any(e["capability"] == cap for e in feed["entries"])

        # only a VERIFIED reachable supplier retires the entry
        card = b'{"protocolVersion":"0.3.0","skills":[{"id":"x"}]}'
        with mock.patch.object(R.socket, "getaddrinfo",
                               return_value=_ai("93.184.216.34")), \
             mock.patch.object(R, "_http_request_pinned",
                               return_value=(200, card)):
            out = store.set_agent_endpoint(
                agent["id"], "https://live.example/a2a", verify=True)
        assert out["recommended_for_routing"] is True
        feed = client.get("/demand/feed").json()
        assert cap not in [e["capability"] for e in feed["entries"]], (
            "verified reachable supply is the ONLY thing that retires demand")


# ---------------------------------------------------------------------------
# 4 — durable dedupe keyed (actor, capability, window)
# ---------------------------------------------------------------------------

def test_dedupe_survives_an_event_flood():
    """The old rule scanned the last 500 events. 600 unrelated events later,
    the same actor re-asking within the window was counted AGAIN."""
    cap = _cap()
    ctx = demand.record_demand(cap, transport="http", actor="actor-flood",
                               ua=EXT_UA)
    assert ctx["counted"] is True
    for i in range(600):
        store.record_event(None, "query", ua="x", endpoint="noise")
    ctx2 = demand.record_demand(cap, transport="http", actor="actor-flood",
                                ua=EXT_UA)
    assert ctx2["counted"] is False, (
        "dedupe must be durable state, not a bounded event-tail scan")
    rows = [e for e in store.events
            if e.get("type") == "capability_demand"
            and e.get("capability") == cap]
    assert len(rows) == 1


def test_dedupe_survives_a_process_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILD_STORE", "json")
    path = str(tmp_path / "guild.json")

    def boot():
        s = Store(path=path)
        monkeypatch.setattr(app_state, "store", s)
        return s

    cap = _cap()
    boot()
    ctx = demand.record_demand(cap, transport="http", actor="actor-restart",
                               ua=EXT_UA)
    assert ctx["counted"] is True
    s2 = boot()                                # process restart
    ctx2 = demand.record_demand(cap, transport="http", actor="actor-restart",
                                ua=EXT_UA)
    assert ctx2["counted"] is False, "dedupe state must survive restarts"
    rows = [e for e in s2.events
            if e.get("type") == "capability_demand"
            and e.get("capability") == cap]
    assert len(rows) == 1


def test_dedupe_is_keyed_by_actor_capability_and_window(monkeypatch):
    cap = _cap()
    assert demand.record_demand(cap, transport="http", actor="a1",
                                ua=EXT_UA)["counted"] is True
    # different actor, same capability → counted
    assert demand.record_demand(cap, transport="http", actor="a2",
                                ua=EXT_UA)["counted"] is True
    # same actor, different capability → counted
    assert demand.record_demand(_cap(), transport="http", actor="a1",
                                ua=EXT_UA)["counted"] is True
    # same actor+capability inside the window → NOT counted
    assert demand.record_demand(cap, transport="http", actor="a1",
                                ua=EXT_UA)["counted"] is False
    # window expiry re-counts (a zero-length window)
    monkeypatch.setenv("GUILD_DEMAND_DEDUPE_S", "0")
    assert demand.record_demand(cap, transport="http", actor="a1",
                                ua=EXT_UA)["counted"] is True
