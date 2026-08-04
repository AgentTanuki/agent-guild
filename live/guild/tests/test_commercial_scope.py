"""The unscoped commercial report must be able to report a paid offer.

The defect: `qualified_exposure` incremented `challenged` and `completed` ONLY
inside its `if operation:` branch, and `/commercial` never accepted an
`operation` at all. So the report published

    qualified_exposure.paid_offers_shown = 0

STRUCTURALLY — not as a measurement of zero exposure, but because no code path
could ever raise it — while telling the reader to "pass `operation` for that"
on a route that silently discarded the parameter. The mandate's LEADING metric
was unobservable through its own scorecard, and unobservable in the flattering
direction: nothing was ever wrong, because nothing could ever be counted.

These tests fail against the old code in three independent ways, so a partial
revert cannot pass them.
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import experiments, pricing  # noqa: E402
from app.store import Store  # noqa: E402

EXT_UA = "a2a:langchain/0.2.1"


@pytest.fixture()
def store(tmp_path) -> Store:
    pricing.load_runtime({})
    return Store(path=str(tmp_path / "guild.json"))


def _challenge(store: Store, operation: str, actor: str, ua: str = EXT_UA):
    store.record_event(actor, "paid_offer_shown", ua=ua,
                       endpoint="x402_challenge",
                       challenged_operation=operation, price_credits=20)


# --------------------------------------------------------------------------
# 1. The portfolio counter must be able to leave zero
# --------------------------------------------------------------------------
def test_unscoped_exposure_counts_challenges_across_operations(store):
    _challenge(store, "deep_preflight", "a2a:net:one", "a2a:langchain/0.2.1")
    _challenge(store, "evidence_bundle", "a2a:net:two", "a2a:crewai/1.0")

    scoped_deep = experiments.qualified_exposure(store, "deep_preflight")
    scoped_bundle = experiments.qualified_exposure(store, "evidence_bundle")
    portfolio = experiments.qualified_exposure(store)

    assert scoped_deep["paid_offers_shown"] == 1
    assert scoped_bundle["paid_offers_shown"] == 1
    # THE regression: this was 0 forever.
    assert portfolio["paid_offers_shown"] == 2, portfolio
    # And the portfolio must equal the sum of its parts, not a looser rule.
    assert portfolio["paid_offers_shown"] == (
        scoped_deep["paid_offers_shown"] + scoped_bundle["paid_offers_shown"])


def test_unscoped_exposure_still_excludes_free_use(store):
    """The counter may leave zero, but only for a price actually shown."""
    for i in range(10):
        store.record_event(f"a2a:net:free{i}", "preflight_run",
                           ua=f"a2a:langchain/0.2.{i}", endpoint="preflight")
    portfolio = experiments.qualified_exposure(store)
    assert portfolio["qualified_events"] == 10
    assert portfolio["paid_offers_shown"] == 0, \
        "free product use is not exposure to a price"


def test_unscoped_exposure_excludes_our_own_traffic(store):
    _challenge(store, "deep_preflight", "a2a:net:bot", "guild-live-conformance")
    assert experiments.qualified_exposure(store)["paid_offers_shown"] == 0


# --------------------------------------------------------------------------
# 2. `operation` must be honoured on the routes, or refused
# --------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "guild.json"))
    from app import main as _main
    return TestClient(_main.app), _main.store


def test_commercial_honours_operation_scope(client):
    """Measured as a DELTA.

    `app.main.store` is a module singleton shared with the whole suite, so an
    absolute count here would pass or fail depending on which tests ran first
    — an order-dependent assertion is not evidence about this code."""
    c, st = client

    def shown(**q):
        r = c.get("/commercial", params=q)
        assert r.status_code == 200, r.text
        return r.json()["qualified_exposure"]

    before_deep = shown(operation="deep_preflight")["paid_offers_shown"]
    before_bundle = shown(operation="evidence_bundle")["paid_offers_shown"]
    before_all = shown()["paid_offers_shown"]

    _challenge(st, "deep_preflight", "a2a:net:scope1", "a2a:langchain/0.2.1")
    _challenge(st, "evidence_bundle", "a2a:net:scope2", "a2a:crewai/1.0")

    after_deep = shown(operation="deep_preflight")
    after_bundle = shown(operation="evidence_bundle")
    after_all = shown()

    assert after_deep["operation_scope"] == "deep_preflight"
    assert after_deep["paid_offers_shown"] - before_deep == 1
    assert after_bundle["paid_offers_shown"] - before_bundle == 1
    # THE regression: unscoped, this delta was 0 whatever happened.
    assert after_all["paid_offers_shown"] - before_all == 2, after_all
    assert c.get("/commercial", params={"operation": "deep_preflight"}
                 ).json()["revenue_first"]["operation_scope"] == "deep_preflight"


def test_unknown_operation_is_refused_not_ignored(client):
    c, _ = client
    for path in ("/commercial", "/funnel/paid"):
        r = c.get(path, params={"operation": "not_a_product"})
        assert r.status_code == 400, (path, r.status_code, r.text)


def test_funnel_paid_honours_operation_scope(client):
    c, st = client

    def funnel(**q):
        r = c.get("/funnel/paid", params=q)
        assert r.status_code == 200, r.text
        return r.json()

    before_deep = funnel(operation="deep_preflight")["raw_impressions"]
    before_all = funnel()["raw_impressions"]

    st.record_event("a2a:net:scope3", "paid_offer_served", ua=EXT_UA,
                    offer="paid", operation="deep_preflight",
                    source="paid_offer:mcp_tool")
    st.record_event("a2a:net:scope4", "paid_offer_served", ua="a2a:crewai/1.0",
                    offer="paid", operation="evidence_bundle",
                    source="paid_offer:mcp_tool")

    scoped = funnel(operation="deep_preflight")
    assert scoped["operation_scope"] == "deep_preflight"
    assert set(scoped["by_operation"]) == {"deep_preflight"}, scoped["by_operation"]
    assert scoped["raw_impressions"] - before_deep == 1

    whole = funnel()
    assert whole["operation_scope"] == "all_operations"
    assert whole["raw_impressions"] - before_all == 2
