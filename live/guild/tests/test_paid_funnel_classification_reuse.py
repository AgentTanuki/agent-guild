"""Faster reporting must preserve the full historical attribution result."""
import itertools

import pytest

from app import attribution
from app.store import Store


def _without_reuse(store, monkeypatch):
    original = attribution.is_genuine_external
    with monkeypatch.context() as patch:
        # Force the established raw-event classification path. This is the
        # historical implementation's behavior, including ownership labels.
        patch.setattr(attribution, "is_genuine_external",
                      lambda event, **kwargs: original(event))
        return store.paid_offer_funnel()


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "report.sqlite3"))
    return Store(path="")


def test_complete_report_matches_raw_event_classification(
        isolated_store, monkeypatch):
    store = isolated_store
    store.accounts.update({
        "member": {"key": "member", "owner_agent_id": "independent"},
        "owned": {"key": "owned", "owner_agent_id": "legacy-owned"},
    })
    store.agents.update({
        "independent": {"id": "independent", "milestones": {"key_proof": True}},
        "legacy-owned": {"id": "legacy-owned", "first_party": True},
    })
    events = itertools.product(
        ["anonymous", "member", "owned"],
        ["mcp:independent/1", "curl/8.7", "python-httpx/0.28",
         "guild-live-conformance", "registry-crawler", "agentguild-skill/1"],
        ["2026-07-21T07:19:59+00:00", "2026-07-21T07:20:00+00:00",
         "2026-07-21T07:40:00+00:00", "2026-07-21T07:40:01+00:00"],
        [{}, {"fp": True}, {"fp_role": "test"}, {"op": True},
         {"origin": "swarm_scout"}, {"actor_distinct": False}],
    )
    for key, ua, at, extra in events:
        # Append synthetic historic rows directly so read-time ownership,
        # rather than current write-time tagging, is what this test exercises.
        event = {"key": key, "ua": ua, "at": at,
                 "type": "paid_offer_served", "fp": False,
                 "operation": "best_agent", "source": "paid_offer:manifest",
                 "actor_distinct": True, **extra}
        if store.backend:
            store.backend.append_event(event)
        else:
            store.events.append(event)
    expected = _without_reuse(store, monkeypatch)
    actual = store.paid_offer_funnel()
    assert actual == expected
    assert actual["raw_impressions"] == 432
    assert actual["qualified_distinct_actors"] == 2

    # A correction to ownership must affect the very next read; no report or
    # event-class cache may keep an old external classification alive.
    store.agents["independent"]["first_party"] = True
    updated = store.paid_offer_funnel()
    assert updated == _without_reuse(store, monkeypatch)
    assert updated["qualified_distinct_actors"] == 1


def test_each_event_is_classified_once_when_no_ownership_override(
        isolated_store, monkeypatch):
    store = isolated_store
    for ua in ["mcp:independent/1", "curl/8.7", "registry-crawler",
               "guild-live-conformance", "agentguild-skill/1"]:
        store.record_event("same-actor", "paid_offer_served", ua=ua,
                           operation="best_agent", source="paid_offer:manifest")
    calls = []
    original = attribution.caller_class

    def tracked(event, **kwargs):
        calls.append(event)
        return original(event, **kwargs)

    monkeypatch.setattr(attribution, "caller_class", tracked)
    report = store.paid_offer_funnel()
    assert report["raw_impressions"] == 5
    assert report["qualified_distinct_actors"] == 1
    assert len(calls) == 5


@pytest.mark.parametrize("at", [
    "2026-07-21T07:19:59+00:00", "2026-07-21T07:20:00+00:00",
    "2026-07-21T07:40:00+00:00", "2026-07-21T07:40:01+00:00",
])
def test_reused_class_preserves_public_feed_aliases_and_incident_bounds(at):
    event = {"actor": "public-actor", "user_agent": "python-httpx/0.28",
             "first_party": False, "at": at}
    classified = attribution.caller_class(event)
    assert attribution.is_genuine_external(event, classified=classified) == \
        attribution.is_genuine_external(event)
    assert attribution.attribution_class(event, classified=classified) == \
        attribution.attribution_class(event)
