"""Retained first-party buyer tests are evidence, never external demand."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app import attribution
from app.store import Store


@pytest.fixture(params=["json", "sqlite"])
def store(request, monkeypatch, tmp_path):
    monkeypatch.setenv("GUILD_STORE", request.param)
    return Store(path=str(tmp_path / "guild.json"))


def stage(store, name):
    return next(s for s in store.conversion_funnel()["stages"]
                if s["stage"] == name)


def test_exact_buyer_ids_are_supported_by_committed_owned_run_evidence():
    repo = Path(__file__).resolve().parents[3]
    expected = {
        "external_txn_helloworld.json": "agent_1e6cf5203b48",
        "external_txn_paki.json": "agent_42e6eb9716d5",
    }
    for filename, agent_id in expected.items():
        run = json.loads((repo / "artifacts/market_evidence" / filename).read_text())
        registered = next(s for s in run["steps"] if s["name"] == "register_buyer")
        assert registered["agent_id"] == agent_id
        assert run["settlement"]["currency"] == "credits_sandbox"
        assert attribution.is_guild_operated_agent(agent_id, {})
        # The independently operated provider is not reclassified as ours.
        assert not attribution.is_guild_operated_agent(run["provider"]["id"], {})


def test_known_owned_either_party_reclassifies_without_rewriting_evidence(store):
    stranger = "agent_unrelated"
    store.agents[stranger] = {"id": stranger, "first_party": False}
    for agent_id in attribution.KNOWN_GUILD_OPERATED_AGENT_IDS:
        store.agents[agent_id] = {"id": agent_id, "first_party": False}
        for role in ("requester", "worker"):
            for state in ("accepted", "delivered"):
                task_id = f"{agent_id}-{role}-{state}"
                store.tasks[task_id] = {
                    "id": task_id,
                    "requester_agent_id": agent_id if role == "requester" else stranger,
                    "worker_agent_id": agent_id if role == "worker" else stranger,
                    "outcome": state, "payment": 3.0,
                    "deliverable_hash": "0x" + "12" * 32,
                }
    before = deepcopy((store.agents, store.tasks, store.billing_log))
    result = stage(store, "outcome")
    count = len(attribution.KNOWN_GUILD_OPERATED_AGENT_IDS) * 2
    assert result["by_outcome"]["accepted"] == {
        "external": 0, "first_party": count, "unknown": 0}
    assert result["by_outcome"]["delivered"] == {
        "external": 0, "first_party": count, "unknown": 0}
    assert result["count"] == 0
    assert (store.agents, store.tasks, store.billing_log) == before
    assert store.agents[stranger]["first_party"] is False


@pytest.mark.parametrize("owner_field", ["owner_agent_id", "agent_id"])
def test_legacy_account_flow_uses_current_ownership_without_rewriting_events(
        store, owner_field):
    owned_id = "agent_1e6cf5203b48"
    store.agents[owned_id] = {"id": owned_id, "first_party": False}
    store.accounts["owned-test-key"] = {owner_field: owned_id, "first_party": False}
    historical = {
        "key": "owned-test-key", "type": "delegation", "fp": False,
        "ua": "python-httpx/0.27", "at": "2026-07-13T14:23:19+00:00",
    }
    store.events.append(historical)
    if store.backend is not None:
        store.backend.append_event(historical)
    assert stage(store, "delegation")["breakdown"] == {
        "external": 0, "first_party": 1, "unknown": 0}
    assert historical["fp"] is False
    store.record_event("owned-test-key", "delegation", ua="python-httpx/0.27")
    assert store.events[-1]["fp"] is True


def test_unproven_similar_buyers_and_external_providers_keep_their_attribution(store):
    # The audit found similar greeting runs, but no exact owned-run artifacts
    # for these two buyers. Similarity must not become an ownership rule.
    provider = "agent_707735e598c5"
    store.agents[provider] = {"id": provider, "first_party": False}
    for agent_id in ("agent_4e44d05783a2", "agent_1864d45db456",
                     "agent_1e6cf5203b49"):
        store.agents[agent_id] = {
            "id": agent_id, "name": "ExternalBuyer-Py-test", "first_party": False}
        store.tasks[agent_id] = {"requester_agent_id": agent_id,
                                 "worker_agent_id": provider, "outcome": "accepted"}
    result = stage(store, "outcome")
    assert result["by_outcome"]["accepted"] == {
        "external": 3, "first_party": 0, "unknown": 0}
    assert "does not prove independent ownership" in result["source"]


@pytest.mark.parametrize("flag_on", ["account", "agent"])
def test_current_ownership_flags_reclassify_old_flow_events(store, flag_on):
    agent_id = "agent_flagged_later"
    store.agents[agent_id] = {"id": agent_id, "first_party": flag_on == "agent"}
    store.accounts["late-flag-key"] = {
        "owner_agent_id": agent_id, "first_party": flag_on == "account"}
    event = {"type": "query", "paid": True, "key": "late-flag-key",
             "fp": False, "ua": "python-httpx/0.27",
             "at": "2026-07-13T14:23:19+00:00"}
    store.events.append(event)
    if store.backend is not None:
        store.backend.append_event(event)
    assert stage(store, "paid_decision")["breakdown"] == {
        "external": 0, "first_party": 1, "unknown": 0}
    assert event["fp"] is False


def test_flagged_and_missing_records_remain_separate(store):
    store.agents["flagged"] = {"id": "flagged", "first_party": True}
    store.tasks["flagged"] = {"requester_agent_id": "flagged",
                               "worker_agent_id": "missing", "outcome": "accepted"}
    store.tasks["unknown"] = {"requester_agent_id": "missing",
                               "worker_agent_id": "missing2", "outcome": "accepted"}
    assert stage(store, "outcome")["by_outcome"]["accepted"] == {
        "external": 0, "first_party": 1, "unknown": 1}
