"""Historical commercial reads remain complete without growing the process heap."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tracemalloc

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import experiments, store as store_module
from app.store import Store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "commercial.sqlite3"))
    monkeypatch.setattr(store_module, "EVENT_RETENTION_TRIGGER", 5)
    monkeypatch.setattr(store_module, "EVENT_RETENTION_TARGET", 3)
    return Store(path="")


def test_view_replays_the_same_sequence_cut_despite_backdated_appends(store):
    store.record_event("buyer", "query", at="2026-09-01", marker=1)
    view, coverage = store.measurement_event_view(types=("query",))
    store.record_event("buyer", "query", at="2026-08-01", marker=2)

    assert [row["marker"] for row in view] == [1]
    assert [row["marker"] for row in view] == [1]
    assert len(view) == coverage["snapshot_events"] == 1
    joined, joined_coverage = store.measurement_event_view(
        keys=("buyer",), through_seq=coverage["snapshot_through_seq"])
    assert [row["marker"] for row in joined] == [1]
    assert joined_coverage["snapshot_through_seq"] == coverage["snapshot_through_seq"]
    fresh, _ = store.measurement_event_view(types=("query",))
    assert [row["marker"] for row in fresh] == [1, 2]


def test_view_keeps_filters_and_lossy_history_warning(store):
    store.record_event("one", "query", at="2026-09-01")
    store.record_event("one", "query", at="2026-09-02")
    store.record_event("two", "query", at="2026-09-02")
    store.record_event("one", "other", at="2026-09-02")
    store.event_history_floor = {"events_omitted": 7}
    view, coverage = store.measurement_event_view(
        types=("query",), keys=("one",), since="2026-09-02")
    assert len(view) == 1
    assert [(event["key"], event["type"], event["at"]) for event in view] == [
        ("one", "query", "2026-09-02")]
    assert coverage["history_complete"] is False
    assert coverage["history_floor"] == {"events_omitted": 7}


def test_uncommitted_writer_cannot_commit_behind_visible_higher_sequence(store):
    store.record_event("buyer", "query", marker=1)
    writer = sqlite3.connect(store.backend.path, isolation_level=None)
    other_writer = sqlite3.connect(store.backend.path, isolation_level=None,
                                   timeout=0)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO events (type, json) VALUES (?, ?)",
            ("query", json.dumps({"type": "query", "marker": 2})))
        view, _ = store.measurement_event_view(types=("query",))
        assert [event["marker"] for event in view] == [1]
        # SQLite's single writer lock prevents a higher seq committing first.
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other_writer.execute("BEGIN IMMEDIATE")
        writer.execute("COMMIT")
        other_writer.execute("BEGIN IMMEDIATE")
        other_writer.execute(
            "INSERT INTO events (type, json) VALUES (?, ?)",
            ("query", json.dumps({"type": "query", "marker": 3})))
        other_writer.execute("COMMIT")
        assert [event["marker"] for event in view] == [1]
        fresh, _ = store.measurement_event_view(types=("query",))
        assert [event["marker"] for event in fresh] == [1, 2, 3]
    finally:
        writer.close()
        other_writer.close()


def test_actor_ua_rotation_is_bounded_without_losing_events(store):
    for number in range(100):
        store.record_event(
            "mcp:net:independent", "paid_offer_served",
            ua=f"mcp:independent-agent/{number}", operation="best_agent")
    actor = store.paid_actor_journeys()["actors"][0]
    assert len(actor["user_agents"]) == 64
    assert actor["user_agents_truncated"] is True
    assert actor["catalogue_offer_entries"] == 100


def test_actor_join_excludes_events_arriving_after_candidate_cut(store, monkeypatch):
    actor = "mcp:net:independent"
    ua = "mcp:independent-agent/1.0"
    store.record_event(actor, "paid_offer_served", ua=ua,
                       operation="best_agent", actor_distinct=True)
    original = store.measurement_event_view
    calls = 0

    def append_after_snapshot(**kwargs):
        nonlocal calls
        result = original(**kwargs)
        calls += 1
        if calls == 1:
            store.record_event(actor, "paid_offer_challenged", ua=ua,
                               challenged_operation="best_agent",
                               at="2020-01-01T00:00:00+00:00")
        return result

    monkeypatch.setattr(store, "measurement_event_view", append_after_snapshot)
    first = store.paid_actor_journeys()
    assert first["actors"][0]["price_challenges"] == 0
    assert first["measurement_coverage"]["journey_snapshot_events"] == 1
    assert store.paid_actor_journeys()["actors"][0]["price_challenges"] == 1


def test_commercial_reports_stream_large_history_without_changing_totals(store):
    # Deliberately carry bulky, irrelevant event metadata. The report must
    # decode one row at a time, never retain 13 MB just to count 800 entries.
    actor = "mcp:net:independent"
    ua = "mcp:independent-agent/1.0"
    with store.backend.transaction():
        for index in range(800):
            store.backend.append_event({
                "key": actor, "ua": ua, "type": "paid_offer_served",
                "operation": "best_agent", "source": "paid_offer:mcp_tool",
                "actor_distinct": True, "at": "2026-09-01T00:00:00+00:00",
                "unused_metadata": "x" * 16384,
            })
    store.record_event(actor, "paid_offer_challenged", ua=ua,
                       challenged_operation="best_agent", actor_distinct=True,
                       at="2026-09-01T01:00:00+00:00")
    store.record_event(actor, "best_agent_served", ua=ua,
                       settlement_mode="x402", settlement_confirmed=True,
                       settlement_mainnet=True, settlement_amount_atomic=10000,
                       payer_attribution="unverified_payer",
                       at="2026-09-01T01:00:01+00:00")

    tracemalloc.start()
    try:
        funnel = store.paid_offer_funnel()
        actors = store.paid_actor_journeys()
        commercial = experiments.snapshot(store, "best_agent")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 4 * 1024 * 1024
    assert funnel["raw_impressions"] == 800
    assert funnel["qualified_distinct_actors"] == 1
    buyer = actors["actors"][0]
    assert buyer["catalogue_offer_entries"] == 800
    assert buyer["price_challenges"] == 1
    assert buyer["paid_completions"] == 1
    assert buyer["independently_attested_external_completions"] == 0
    assert buyer["returned"] is True
    assert commercial["commercial"]["external_settled_revenue_usd"] == pytest.approx(.01)
    assert commercial["commercial"]["attributed_external_payments"] == 0
    assert "unused_metadata" not in json.dumps([funnel, actors, commercial])
