"""A supplier must not mistake owned checks or lost history for real demand."""
import json

import pytest
from fastapi.testclient import TestClient

from app import a2a, main, store as store_module
from app.store import Store
from app.swarm import agents


@pytest.fixture(params=["json", "sqlite"])
def isolated_store(request, tmp_path, monkeypatch):
    monkeypatch.setenv("GUILD_STORE", request.param)
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "history.sqlite3"))
    s = Store(path="")
    monkeypatch.setattr(main, "store", s)
    monkeypatch.setattr(a2a, "store", s)
    return s


def ask(s, actor, cap, *, at="2026-09-01T10:00:00+00:00", **fields):
    s.record_event(actor, "capability_demand", ua=fields.pop("ua", "langchain/0.3"),
                   capability=cap, actor=actor, at=at, explicit=True,
                   transport="http", **fields)


def maps():
    client = TestClient(main.app)
    http = client.get("/capabilities").json()
    response = client.post("/a2a", json={
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {"parts": [{"kind": "text", "text": "capabilities"}]}},
    }).json()
    a2a = json.loads(response["result"]["parts"][0]["text"])
    return http, a2a


def test_owned_and_unattributable_asks_are_not_supplier_opportunities(isolated_store):
    s = isolated_store
    ask(s, "owned", "owned-only", demand_first_party=True,
        caller_proof_verified=True)
    ask(s, "fixture", "test-only", fp=True, fp_role="test")
    ask(s, "crawler", "crawl-only", ua="Glama-Bot/2.0 (+crawler)")
    ask(s, "tool", "tool-only", ua="curl/8.5")
    ask(s, "buyer", "wanted")
    retained = [dict(e) for e in s.events if e["type"] == "capability_demand"]
    http, a2a = maps()
    assert set(http["unmet_demand"]) == {"wanted"}
    assert set(a2a["demand"]) == {"wanted"}
    assert [e for e in s.events if e["type"] == "capability_demand"] == retained


def test_owned_traffic_does_not_inflate_counts_or_refresh_buyer_recency(isolated_store):
    s = isolated_store
    ask(s, "buyer", "mixed", supplied=True)
    ask(s, "buyer", "mixed", supplied=True)  # same actor and hour: one ask
    ask(s, "owned", "mixed", at="2026-09-15T18:00:00+00:00",
        demand_first_party=True, supplied=True)
    http, a2a = maps()
    for row in [http["unmet_demand"]["mixed"], a2a["demand"]["mixed"]]:
        assert row["lookups"] == row["supplied_lookups"] == 1
        assert row["last_lookup"] == "2026-09-01T10:00:00+00:00"
        assert row["verified_lookups"] == 0
        assert row["heuristic_lookups"] == 1
    for result in [http, a2a]:
        measurement = result["demand_measurement"]
        assert measurement["measurement_version"] == "capability-demand-summary-v2"
        assert "not funded jobs" in measurement["interpretation"]


def test_verified_identity_and_legacy_asks_remain_distinct(isolated_store):
    s = isolated_store
    ask(s, "signed", "signed-work", ua="curl/8.5", caller_proof_verified=True)
    s.record_event("old-buyer", "query", ua="a2a:langchain/0.3",
                   actor="old-buyer", endpoint="a2a_message",
                   caller_kind="capability_ask", capability="legacy-work",
                   at="2026-08-01T10:00:00+00:00")
    summary = s.demand_summary()
    assert summary["signed-work"]["verified_lookups"] == 1
    assert summary["signed-work"]["heuristic_lookups"] == 0
    assert summary["legacy-work"]["heuristic_lookups"] == 1
    assert summary["legacy-work"]["provenance"] == ["legacy_derived_heuristic"]


def test_durable_demand_survives_retention_with_honest_json_coverage(
        isolated_store, monkeypatch):
    s = isolated_store
    monkeypatch.setattr(store_module, "EVENT_RETENTION_TRIGGER", 5)
    monkeypatch.setattr(store_module, "EVENT_RETENTION_TARGET", 3)
    ask(s, "buyer", "retained-work")
    for i in range(8):
        s.record_event(None, "unrelated_activity", n=i)
    assert s.events_omitted_by_retention > 0
    if s.backend is not None:
        monkeypatch.setattr(s.backend, "fetch_events",
                            lambda **kw: pytest.fail("unbounded history fetch"))
    result = TestClient(main.app).get("/capabilities").json()
    coverage = result["demand_measurement"]["measurement_coverage"]
    if s.backend is not None:
        assert result["unmet_demand"]["retained-work"]["lookups"] == 1
        assert coverage["history_complete"] is True
        assert coverage["read_mode"] == "bounded_memory"
    else:
        assert coverage["history_complete"] is False
        assert "retained-work" not in result["unmet_demand"]


def test_gap_scout_uses_qualified_requests_not_raw_query_labels(isolated_store):
    s = isolated_store
    s.record_event("owned", "query", ua="guild-ops-check/1", fp=True,
                   endpoint="a2a_message", caller_kind="capability_ask",
                   capability="owned-proposal")
    ask(s, "buyer", "actual-proposal")
    result = agents._tick_gap_scout(s, None, None)
    assert result["capability_proposals"] == ["actual-proposal"]


def test_authenticated_empty_search_check_is_not_advertised_as_demand(
        isolated_store, monkeypatch):
    monkeypatch.setenv("GUILD_FIRST_PARTY_TOKEN", "fixture-owned-token")
    client = TestClient(main.app)
    response = client.get("/search?capability=owned-empty-search", headers={
        "User-Agent": "langchain/0.3",
        "X-Agent-Guild-First-Party": "fixture-owned-token",
        "X-Agent-Guild-Role": "test",
    })
    assert response.status_code == 200 and response.json()["count"] == 0
    assert any(e.get("capability") == "owned-empty-search"
               for e in isolated_store.events)
    assert "owned-empty-search" not in client.get("/capabilities").json()["unmet_demand"]
