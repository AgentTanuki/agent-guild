"""Qualified paid-funnel actors have a durable, non-secret journey view."""
from __future__ import annotations

import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import main, paidcatalog  # noqa: E402
from app.store import Store  # noqa: E402


OPS = [str(operation["operation"]) for operation in paidcatalog.operations()]


def _catalogue(store: Store, actor: str, ua: str, at: str) -> None:
    for operation in OPS:
        store.record_event(
            actor, "paid_offer_served", ua=ua, at=at,
            operation=operation, source="paid_offer:mcp_tool",
            actor_distinct=True, endpoint="mcp_tool", transport="mcp",
            price_credits=10,
        )


def test_paid_actor_journeys_separates_catalogue_quotes_and_completion(tmp_path):
    store = Store(path=str(tmp_path / "guild.json"))
    returning = "mcp:net:external-one"
    catalog_only = "mcp:net:external-two"
    returning_ua = "mcp:independent-wallet-agent/1.2"
    catalog_ua = "mcp:another-autonomous-agent/0.4"

    _catalogue(store, returning, returning_ua, "2026-08-15T00:00:00+00:00")
    store.record_event(
        returning, "paid_offer_shown", ua=returning_ua,
        at="2026-08-15T00:00:05+00:00", endpoint="x402_challenge",
        transport="mcp", actor_distinct=True,
        challenged_operation="payment_decision", price_credits=10,
    )
    _catalogue(store, returning, returning_ua, "2026-08-15T01:00:00+00:00")
    _catalogue(store, catalog_only, catalog_ua, "2026-08-15T00:10:00+00:00")

    # Our own release client must never enter the view even when it reads the
    # exact same catalogue.
    _catalogue(
        store, "mcp:net:owned", "mcp:guild-live-conformance/1.0",
        "2026-08-15T00:20:00+00:00")

    report = store.paid_actor_journeys()
    assert report["qualified_distinct_actors"] == 2
    assert report["measurement_coverage"]["history_complete"] is True
    by_actor = {actor["actor"]: actor for actor in report["actors"]}

    first = by_actor[returning]
    assert first["catalogue_offer_entries"] == 2 * len(OPS)
    assert first["mcp_catalogue_offer_entries"] == 2 * len(OPS)
    assert first["mcp_catalogue_distinct_operations"] == sorted(OPS)
    assert first["price_challenges"] == 1
    assert first["challenged_operations"] == {"payment_decision": 1}
    assert first["paid_completions"] == 0
    assert first["verified_mainnet_completions"] == 0
    assert first["independently_attested_external_completions"] == 0
    assert first["visits_30m"] == 2
    assert first["returned"] is True
    assert first["signal"] == "quoted_no_completion"

    second = by_actor[catalog_only]
    assert second["mcp_catalogue_offer_entries"] == len(OPS)
    assert second["price_challenges"] == 0
    assert second["visits_30m"] == 1
    assert second["returned"] is False
    assert second["signal"] == "catalogue_only"
    assert "mcp:net:owned" not in by_actor


def test_paid_actor_journeys_distinguishes_non_revenue_from_real_revenue(tmp_path):
    store = Store(path=str(tmp_path / "guild.json"))
    actor = "mcp:net:external-three"
    ua = "mcp:wallet-runtime/3.1"
    _catalogue(store, actor, ua, "2026-08-15T00:00:00+00:00")
    store.record_event(
        actor, "payment_decision_issued", ua=ua,
        at="2026-08-15T00:00:10+00:00", transport="mcp",
        settlement_mode="credits_sandbox", settlement_confirmed=False,
        settlement_mainnet=False,
    )
    journey = store.paid_actor_journeys()["actors"][0]
    assert journey["signal"] == "non_revenue_completion"

    store.record_event(
        actor, "payment_decision_issued", ua=ua,
        at="2026-08-15T00:00:20+00:00", transport="mcp",
        settlement_mode="x402", settlement_confirmed=True,
        settlement_mainnet=True, payer_attribution="unverified_payer",
    )

    journey = store.paid_actor_journeys()["actors"][0]
    assert journey["paid_completions"] == 2
    assert journey["verified_mainnet_completions"] == 1
    assert journey["independently_attested_external_completions"] == 0
    assert journey["signal"] == "verified_mainnet_unattributed_completion"

    store.record_event(
        actor, "payment_decision_issued", ua=ua,
        at="2026-08-15T00:00:30+00:00", transport="mcp",
        settlement_mode="x402", settlement_confirmed=True,
        settlement_mainnet=True,
        payer_attribution="independently_attested_external_machine",
    )
    journey = store.paid_actor_journeys()["actors"][0]
    assert journey["independently_attested_external_completions"] == 1
    assert journey["signal"] == "independently_attested_external_completion"


def test_paid_actor_journeys_ignore_unrelated_history_and_bad_time(tmp_path):
    store = Store(path=str(tmp_path / "guild.json"))
    actor = "mcp:net:bounded"
    ua = "mcp:bounded-agent/1.0"
    _catalogue(store, actor, ua, "2026-08-15T00:00:00+00:00")
    store.record_event(
        actor, "query", ua=ua, at="not-a-time", transport="mcp")
    for i in range(100):
        store.record_event(
            f"unrelated-{i}", "query", ua="browser/1.0", marker=i)

    report = store.paid_actor_journeys()
    assert report["qualified_distinct_actors"] == 1
    assert report["actors"][0]["event_types"]["query"] == 1
    assert report["measurement_coverage"]["candidate_snapshot_events"] == len(OPS)
    assert report["measurement_coverage"]["journey_snapshot_events"] == len(OPS) + 1


def test_public_actor_journey_route_uses_the_same_store(monkeypatch, tmp_path):
    store = Store(path=str(tmp_path / "guild.json"))
    _catalogue(
        store, "mcp:net:route-actor", "mcp:external-route-agent/1.0",
        "2026-08-15T00:00:00+00:00")
    monkeypatch.setattr(main, "store", store)

    response = TestClient(main.app).get("/funnel/paid/actors")

    assert response.status_code == 200
    body = response.json()
    assert body["qualified_distinct_actors"] == 1
    assert body["actors"][0]["actor"] == "mcp:net:route-actor"
    assert "sk_" not in response.text
    assert "request_body" not in response.text
    assert "paymentpayload" not in response.text.lower()
