"""Commercial truth must outlive the bounded in-process event cache."""
from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import experiments, pricing  # noqa: E402
from app import store as store_module  # noqa: E402
from app.store import Store  # noqa: E402


@pytest.fixture()
def sqlite_store(tmp_path, monkeypatch) -> Store:
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "events.sqlite3"))
    monkeypatch.setattr(store_module, "EVENT_RETENTION_TRIGGER", 5)
    monkeypatch.setattr(store_module, "EVENT_RETENTION_TARGET", 3)
    pricing.load_runtime({})
    return Store(path="")


def _external_completion(store: Store, *, key: str, attribution: str,
                         mainnet: bool = True, confirmed: bool = True) -> None:
    store.record_event(
        key, "best_agent_served", ua="langchain/0.2.1",
        endpoint="check", price_credits=10, paid=True,
        settlement_mode="x402", settlement_confirmed=confirmed,
        settlement_mainnet=mainnet,
        settlement_network="eip155:8453" if mainnet else "eip155:84532",
        settlement_amount_atomic=10000,
        payer_attribution=attribution)


def test_funnel_experiment_and_revenue_read_durable_history(sqlite_store):
    s = sqlite_store
    s.record_event(
        "http:buyer", "paid_offer_served", ua="langchain/0.2.1",
        operation="best_agent", source="http")
    s.record_event(
        "http:buyer", "paid_offer_shown", ua="langchain/0.2.1",
        challenged_operation="best_agent", actor_distinct=True,
        impression="challenge_402", price_credits=10)
    _external_completion(
        s, key="http:buyer",
        attribution="independently_attested_external_machine")
    # These three settlements exercise every fail-closed exclusion after the
    # same records have fallen out of serving memory.
    _external_completion(s, key="http:buyer",
                         attribution="independently_attested_external_machine",
                         mainnet=False)
    _external_completion(s, key="http:buyer",
                         attribution="independently_attested_external_machine",
                         confirmed=False)
    _external_completion(s, key="http:buyer",
                         attribution="verified_first_party_canary")
    for i in range(8):
        s.record_event(None, "query", ua=f"filler/{i}")

    assert all(event["type"] == "query" for event in s.events)
    assert s.events_omitted_by_retention > 0

    funnel = s.paid_offer_funnel("best_agent")
    assert funnel["qualified_distinct_actors"] == 1
    assert funnel["raw_impressions"] == 1
    assert funnel["measurement_coverage"]["source"] == "sqlite_durable"
    assert funnel["measurement_coverage"]["history_complete"] is True

    exposure = experiments.qualified_exposure(s, "best_agent")
    assert exposure["qualified_actors"] == 1
    assert exposure["paid_offers_shown"] == 1
    assert exposure["measurement_coverage"]["source"] == "sqlite_durable"

    revenue = experiments.commercial_metrics(s, "best_agent")
    assert revenue["external_settled_revenue_usd"] == pytest.approx(0.01)
    assert revenue["distinct_external_payers"] == 1
    assert revenue["paid_decisions"] == 1
    assert revenue["supporting_testnet_or_unconfirmed_NOT_REVENUE"] == 2
    # revenue-semantics correction 2026-08-31: the canary completion is
    # positively first-party (excluded, shown as first-party money), and the
    # attested completion is revenue AND attributed - nothing here is
    # settled-but-unattributable.
    assert revenue["settled_but_not_attributable_external"] == 0
    assert revenue["known_first_party_settled_usd"] == pytest.approx(0.01)
    assert revenue["attributed_external_payments"] == 1


def test_objective_metrics_use_durable_history_not_serving_tail(sqlite_store):
    s = sqlite_store
    parent = "a" * 64
    s.record_event(
        "a2a:buyer", "query", ua="a2a:test", endpoint="a2a_message",
        caller_kind="objective_ask", capability="fact-check",
        request_sha256=parent, request_utf8_bytes=24)
    s.record_event(
        "a2a:buyer", "first_contact_response", ua="a2a:test",
        endpoint="a2a_message", caller_kind="objective_ask",
        capability="fact-check", request_sha256=parent,
        response_bytes=640, response_kind="objective_match")
    s.record_event(
        "a2a:buyer", "objective_action_followed", ua="a2a:test",
        action="trust.check.full", parent_request_sha256=parent,
        capability="fact-check")
    for i in range(8):
        s.record_event(None, "filler", i=i)

    assert s.events_omitted_by_retention > 0
    assert not any(event.get("request_sha256") == parent
                   for event in s.events)
    metrics = s.objective_to_action_funnel()
    assert metrics["objective_requests"] == 1
    assert metrics["mapped"] == 1
    assert metrics["response_bytes"]["observed"] == 1
    assert metrics["full_detail_followthrough"]["followed"] == 1
    retention = metrics["retention"]
    assert retention["measurement_source"] == "sqlite_durable"
    assert retention["history_complete"] is True
    assert retention["events_omitted"] == 0
    assert retention["in_memory_tail_omitted"] > 0


def test_running_arm_and_price_state_survive_restart_unchanged(sqlite_store):
    s = sqlite_store
    baseline = {metric: 0 for metric in experiments.PRIMARY_METRICS}
    rec = experiments.define(
        s, "machine", hypothesis="machine envelope conversion",
        variable="price:machine_envelope", baseline=baseline,
        tested_price_credits=10)
    rec["started_at"] = "2026-08-15T00:00:00+00:00"
    rec["min_qualified"] = 7
    s.experiments["machine"] = rec
    s.price_overrides["machine_envelope"] = 10
    with s.lock, s._txn():
        s._persist_kv("experiments", s.experiments)
        s._persist_kv("price_overrides", s.price_overrides)

    before_experiment = copy.deepcopy(s.experiments["machine"])
    before_overrides = copy.deepcopy(s.price_overrides)
    restarted = Store(path="")
    experiments.seed_defaults(restarted)

    assert restarted.experiments["machine"] == before_experiment
    assert restarted.price_overrides == before_overrides


def test_one_experiment_decision_uses_one_durable_event_cut(
        sqlite_store, monkeypatch):
    s = sqlite_store
    rec = experiments.define(
        s, "best", hypothesis="best-agent conversion",
        variable="price:best_agent",
        baseline={metric: 0 for metric in experiments.PRIMARY_METRICS},
        tested_price_credits=10)
    rec["min_qualified"] = 1
    s.experiments["best"] = rec
    s.record_event(
        "http:buyer", "paid_offer_shown", ua="langchain/0.2.1",
        challenged_operation="best_agent", actor_distinct=True,
        impression="challenge_402", price_credits=10)

    original = s.backend.fetch_events
    calls = []

    def _fetch_events(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(s.backend, "fetch_events", _fetch_events)
    out = experiments.evaluate(s, "best")
    assert out["decision"] == "kill"
    assert len(calls) == 1
    assert set(calls[0]["types"]) == {
        "paid_offer_shown", "paid_offer_challenged", "best_agent_served"}
