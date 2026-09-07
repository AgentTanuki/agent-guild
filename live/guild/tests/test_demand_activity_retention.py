"""Demand and execution evidence must survive serving-cache retention."""
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import store as store_module
from app.store import Store
from app.swarm import graph


@pytest.fixture()
def retained_store(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "history.sqlite3"))
    monkeypatch.setattr(store_module, "EVENT_RETENTION_TRIGGER", 5)
    monkeypatch.setattr(store_module, "EVENT_RETENTION_TARGET", 3)
    return Store(path="")


def trim(store):
    for i in range(8):
        store.record_event(None, "unrelated_activity", n=i)
    assert store.events_omitted_by_retention > 0
    assert all(e["type"] == "unrelated_activity" for e in store.events)


def test_unmet_demand_survives_retention(retained_store):
    s = retained_store
    s.record_event("buyer", "capability_demand", ua="langchain/0.3",
                   capability="retained-work", actor="buyer", explicit=True,
                   transport="http")
    trim(s)
    rows = s.demand_feed_entries()
    assert len(rows) == 1
    assert rows[0]["capability"] == "retained-work"
    assert rows[0]["heuristic_lookups"] == 1


def test_invocations_and_flow_survive_retention(retained_store):
    s = retained_store
    for _ in range(2):
        s.record_event("buyer", "swarm_invoke", ua="langchain/0.3",
                       actor="buyer", capability="json.repair", outcome="success")
    s.record_event("buyer", "delegation", ua="langchain/0.3")
    trim(s)
    activity = graph.growth_stats(s)["genuine_external"]
    assert activity["total_invocations"] == 2
    assert activity["successful_completions"] == 2
    assert activity["repeat_callers"] == 1
    assert graph.build_graph(s)["actors"][0]["invocations"] == 2
    stage = next(v for v in s.conversion_funnel()["stages"]
                 if v["stage"] == "delegation")
    assert stage["count"] == 1


def test_durable_reports_stream_one_cut_and_preserve_exclusions(
        retained_store, monkeypatch):
    s = retained_store
    s.record_event("buyer", "query", ua="a2a:langchain/0.3",
                   endpoint="a2a_message", caller_kind="capability_ask",
                   capability="wanted")
    s.record_event("buyer", "capability_demand", ua="langchain/0.3",
                   actor="buyer", capability="wanted", explicit=True,
                   caller_proof_verified=True, transport="http")
    s.record_event("owned", "capability_demand", ua="langchain/0.3",
                   actor="owned", capability="wanted", explicit=True,
                   fp=True, demand_first_party=True, transport="http")
    s.record_event("owned", "swarm_invoke", ua="langchain/0.3",
                   actor="owned", fp=True, outcome="success")
    trim(s)
    # No report may materialise the entire durable event table.
    monkeypatch.setattr(s.backend, "fetch_events", lambda **kw: pytest.fail("unbounded fetch"))
    original = s.measurement_event_view

    def append_after_cut(**kw):
        events, coverage = original(**kw)
        s.record_event("later", "query", ua="a2a:langchain/0.3",
                       endpoint="a2a_message", caller_kind="capability_ask",
                       capability="later-ask")
        return events, coverage

    monkeypatch.setattr(s, "measurement_event_view", append_after_cut)
    report = s.demand_feed_report()
    assert [r["capability"] for r in report["entries"]] == ["wanted"]
    row = report["entries"][0]
    assert row["lookups"] == 2  # canonical replaces same-actor legacy ask
    assert row["genuine_lookups"] == row["verified_lookups"] == 1
    assert row["heuristic_lookups"] == 0
    assert report["measurement_coverage"]["history_complete"] is True
    assert report["measurement_coverage"]["read_mode"] == "bounded_memory"
    monkeypatch.setattr(s, "measurement_event_view", original)
    stats = graph.growth_stats(s)
    assert stats["genuine_external"]["total_invocations"] == 0
    assert stats["ag_internal_first_party"]["total_invocations"] == 1
    assert graph.build_graph(s)["actors"][0]["organic"] is False
    assert stats["cost_per_successful_external_acquisition"] is None
    assert s.conversion_funnel()["measurement_coverage"]["history_complete"] is True


def test_restart_keeps_activity_and_coverage(retained_store):
    s = retained_store
    s.record_event("buyer", "swarm_invoke", ua="langchain/0.3",
                   actor="buyer", outcome="success")
    trim(s)
    restarted = Store(path="")
    stats = graph.growth_stats(restarted)
    assert stats["genuine_external"]["total_invocations"] == 1
    assert stats["measurement_coverage"]["source"] == "sqlite_durable"
    assert stats["measurement_coverage"]["history_complete"] is True


@pytest.mark.parametrize("backend", ["json", "sqlite"])
def test_missing_prefix_is_never_presented_as_complete(
        tmp_path, monkeypatch, backend):
    monkeypatch.setenv("GUILD_STORE", backend)
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "floor.sqlite3"))
    monkeypatch.setattr(store_module, "EVENT_RETENTION_TRIGGER", 5)
    monkeypatch.setattr(store_module, "EVENT_RETENTION_TARGET", 3)
    s = Store(path="")
    trim(s)
    if backend == "sqlite":
        s.event_history_floor = {"reason": "lossy_json_cutover", "omitted_events": 8}
    reports = [s.demand_feed_report(), s.conversion_funnel(),
               graph.growth_stats(s), graph.build_graph(s)]
    for report in reports:
        assert report["measurement_coverage"]["history_complete"] is False


@pytest.mark.parametrize("path", [
    "/terms.json", "/.well-known/ag-identities/index.json", "/identities/fixture",
    "/invoke/json.repair", "/invoke/unknown-capability",
])
def test_first_party_labels_are_durable_before_any_later_save(
        retained_store, monkeypatch, path):
    from app.main import app
    from app.swarm import router, gateway
    registry = router.registry
    s = retained_store
    monkeypatch.setenv("GUILD_FIRST_PARTY_TOKEN", "fixture-owned-token")
    monkeypatch.setattr(router, "store", s)
    monkeypatch.setattr(router, "ensure_built", lambda: None)
    doc = {"identity": {"ag_id": "ag:fixture", "capability": {"id": "json.repair"}}}
    monkeypatch.setattr(registry, "index", lambda base: {"identities": []})
    monkeypatch.setattr(registry, "get", lambda ag_id: doc)
    monkeypatch.setattr(registry, "for_capability", lambda cap: doc if cap == "json.repair" else None)
    monkeypatch.setattr(gateway, "run_capability", lambda *a: ({"fixture": True}, 0.1))
    headers = {"User-Agent": "langchain/0.3", "X-Agent-Guild-First-Party": "fixture-owned-token",
               "X-Agent-Guild-Role": "test"}
    client = TestClient(app)
    response = (client.post(path, json={"text": "{}"}, headers=headers)
                if path.startswith("/invoke/") else client.get(path, headers=headers))
    assert response.status_code == (404 if path.endswith("unknown-capability") else 200)
    # Read immediately from storage: an in-memory post-append tag would fail.
    events = list(s.backend.iter_events())
    event = next(e for e in reversed(events) if e["type"].startswith("swarm_"))
    assert event["fp"] is True
    assert event["fp_role"] == "test"
    assert "fixture-owned-token" not in str(events)
    assert graph.growth_stats(s)["genuine_external"]["total_invocations"] == 0


def test_feed_coverage_is_signed_and_conditional_fetch_remains_cacheable(
        retained_store, monkeypatch):
    from app import main, crypto, x402_artifacts as artifacts
    s = retained_store
    monkeypatch.setattr(main, "store", s)
    s.record_event("buyer", "capability_demand", ua="langchain/0.3",
                   actor="buyer", capability="wanted", explicit=True)
    trim(s)
    request = Request({"type": "http", "headers": []})
    first = main.demand_feed(request, page=1, per_page=50)
    import json
    body = json.loads(first.body)
    integrity = body.pop("integrity")
    assert body["measurement_coverage"]["history_complete"] is True
    assert integrity["content_sha256"] == artifacts.sha256_hex(
        crypto.canonicalize_jcs(body).encode("utf-8"))
    # An unrelated event advances the global sequence, not this feed content.
    s.record_event(None, "unrelated_activity")
    request = Request({"type": "http", "headers": [
        (b"if-none-match", first.headers["etag"].encode())]})
    second = main.demand_feed(request, page=1, per_page=50)
    assert second.status_code == 304


def test_funnel_preserves_real_receipt_states_without_promoting_stops(retained_store):
    from app import evidence_semantics
    s = retained_store
    s.agents.update({"buyer": {"id": "buyer"}, "worker": {"id": "worker"},
                     "owned": {"id": "owned", "first_party": True}})
    states = evidence_semantics.RECEIPT_OUTCOMES | {"success", "failure"}
    for state in states | {"open", "invented"}:
        s.tasks[state] = {"outcome": state, "requester_agent_id": "buyer",
                          "worker_agent_id": "worker"}
    s.tasks["owned-receipt"] = {"outcome": "accepted", "requester_agent_id": "buyer",
                                "worker_agent_id": "owned"}
    s.tasks["missing-party"] = {"outcome": "delivered", "requester_agent_id": "missing",
                               "worker_agent_id": "worker"}
    result = next(r for r in s.conversion_funnel()["stages"] if r["stage"] == "outcome")
    assert result["count"] == len(states)
    assert result["breakdown"] == {"external": len(states), "first_party": 1, "unknown": 1}
    assert result["by_outcome"]["accepted"] == {"external": 1, "first_party": 1, "unknown": 0}
    assert result["by_outcome"]["delivered"] == {"external": 1, "first_party": 0, "unknown": 1}
    assert result["by_outcome"]["blocked"]["external"] == 1
    assert "open" not in result["by_outcome"]
    assert "invented" not in result["by_outcome"]


def test_pending_demand_precedes_historical_rank_without_bypassing_limits(
        retained_store, monkeypatch):
    from app.swarm import scout, runner
    s = retained_store
    monkeypatch.setenv("GUILD_SCOUT_AUTORUN", "1")
    monkeypatch.setenv("GUILD_SCOUT_WAKE_DEBOUNCE_S", "0")
    monkeypatch.setattr(scout, "MAX_CAPABILITIES_PER_RUN", 2)
    monkeypatch.setattr(scout, "ADAPTERS", {"fixture": lambda cap, fetch: []})
    # Feed order favours established historical asks. New queued demand must
    # still reach the adapters and be acknowledged after processing.
    rows = [{"capability": f"historical-{n}", "genuine_lookups": 100}
            for n in range(12)]
    rows += [{"capability": cap, "genuine_lookups": qualified}
             for cap, qualified in (("second", 1), ("first", 1), ("not-qualified", 0))]
    monkeypatch.setattr(s, "demand_feed_entries", lambda: rows)
    runner.notify_demand(s, "first")
    runner.notify_demand(s, "second")
    runner.notify_demand(s, "not-qualified")
    out = runner.run_once(s, fetch=lambda *a, **kw: pytest.fail("unexpected network"))
    assert out["completed"] is True
    assert out["summary"]["capabilities"] == ["first", "second"]
    assert set(runner.pending_demand(s)) == {"not-qualified"}
