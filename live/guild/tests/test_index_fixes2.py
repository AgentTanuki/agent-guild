"""Second-round corrections — system-level defects the unit tests missed.

Every defect here passed its own tests. They were found by asking what the
SYSTEM does end to end, which is a different question from what each function
returns:

  1. an A2A deep-preflight challenge settled the wrong operation and returned
     the wrong product — challenge and card tests both passed
  2. `settlement_mode == "x402"` was treated as money, but the same rail
     defaults to Base Sepolia, where a successful settlement pays nothing
  3. any global metric moving could promote any experiment, so unrelated
     escrow revenue would "prove" a deep-preflight price change worked
  4. the A2A agent card still opened with passport issuance

The tests below are end-to-end or attribution-scoped for exactly that reason.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import experiments, payments, pricing  # noqa: E402
from app.store import Store  # noqa: E402


@pytest.fixture()
def store(tmp_path) -> Store:
    pricing.load_runtime({})
    return Store(path=str(tmp_path / "guild.json"))


def _settled_event(store: Store, etype="deep_preflight_run", *, key="a2a:net:payer",
                   ua="a2a:langchain/0.2.1", mainnet=True, confirmed=True,
                   mode="x402", usd_atomic=20000, **extra):
    store.record_event(key, etype, ua=ua, endpoint="preflight_deep",
                       paid=(mode == "x402"), settlement_mode=mode,
                       settlement_confirmed=confirmed,
                       settlement_mainnet=mainnet,
                       settlement_network="eip155:8453" if mainnet else "eip155:84532",
                       settlement_amount_atomic=usd_atomic, **extra)


# --------------------------------------------------------------------------
# 1. A2A payment must settle the operation that was QUOTED
# --------------------------------------------------------------------------
def test_a_deep_preflight_task_rebuilds_as_deep_preflight(store, monkeypatch):
    """The defect: _preq_from_task always rebuilt check_request(capability), so
    a deep-preflight challenge settled the wrong canonical operation."""
    from app import a2a_x402
    monkeypatch.setattr(a2a_x402, "store", store)
    preq = payments.deep_preflight_request("https://x.example/a2a")
    task = a2a_x402.build_payment_required_task(preq, preq.cost,
                                                actor="a2a:net:abc", ua="a2a:ua")
    stored = store.x402_task_get(task["id"])
    assert stored["operation"] == "deep_preflight"
    assert stored["operation_params"]["url"] == "https://x.example/a2a"
    rebuilt = a2a_x402._preq_from_task(stored)
    assert rebuilt.operation == "deep_preflight"
    assert rebuilt.resource_url == preq.resource_url
    assert rebuilt.request_hash == preq.request_hash


def test_rebuild_uses_only_stored_fields_not_the_submission(store, monkeypatch):
    """A caller must not be able to steer settlement onto another operation."""
    from app import a2a_x402
    monkeypatch.setattr(a2a_x402, "store", store)
    preq = payments.deep_preflight_request("https://x.example/a2a")
    task = a2a_x402.build_payment_required_task(preq, preq.cost)
    stored = dict(store.x402_task_get(task["id"]))
    # a hostile echo of a cheaper operation
    stored_with_noise = {**stored, "capability": "fact-check"}
    assert a2a_x402._preq_from_task(stored_with_noise).operation == "deep_preflight"


def test_unknown_operation_is_refused_not_defaulted():
    with pytest.raises(ValueError):
        payments.request_from_stored("not_a_real_operation", {})


def test_full_a2a_payment_round_trip_returns_the_DEEP_product(store, monkeypatch):
    """payment-required -> payment-submitted -> completed, end to end.

    The challenge/card tests passed while this path returned a capability
    shortlist to someone who paid for a deep endpoint check."""
    from app import a2a_x402, deepcheck, x402

    monkeypatch.setattr(a2a_x402, "store", store)
    monkeypatch.setattr(deepcheck, "deep_preflight",
                        lambda s, url: {"tier": "deep", "target": url,
                                        "policy": {"decision": "allow"}})
    preq = payments.deep_preflight_request("https://x.example/a2a")
    task = a2a_x402.build_payment_required_task(
        preq, preq.cost, actor="a2a:net:payer", ua="a2a:langchain/0.2.1")
    stored = store.x402_task_get(task["id"])

    class _Settled:
        record = {"confirmed": True, "mainnet": True, "network": "eip155:8453",
                  "amount_atomic": 20000, "transaction": "0x" + "a" * 64}

        def finalize(self, body):
            return {"settle_response": {}, "extensions": {}}

    out = a2a_x402._produce_for(a2a_x402._preq_from_task(stored),
                                _Settled(), stored)
    assert out["tier"] == "deep", "the caller must receive what they paid for"
    assert out["target"] == "https://x.example/a2a"
    ev = [e for e in store.events if e["type"] == "deep_preflight_run"]
    assert len(ev) == 1, "the settled deep check must be recorded"
    assert ev[0]["settlement_mode"] == "x402"
    assert ev[0]["settlement_confirmed"] is True
    assert ev[0]["settlement_mainnet"] is True
    assert ev[0]["key"] == "a2a:net:payer", "settlement must be attributable"


def test_a_capability_task_still_returns_the_capability_product(store, monkeypatch):
    from app import a2a_x402
    monkeypatch.setattr(a2a_x402, "store", store)
    preq = payments.check_request("fact-check")
    task = a2a_x402.build_payment_required_task(preq, preq.cost)
    stored = store.x402_task_get(task["id"])
    assert a2a_x402._preq_from_task(stored).operation == "best_agent"


# --------------------------------------------------------------------------
# 2. x402 is not the same thing as money
# --------------------------------------------------------------------------
def test_testnet_settlement_can_never_count_as_revenue(store):
    """Base Sepolia is the DEFAULT network. A successful settlement there is a
    successful payment of nothing."""
    _settled_event(store, mainnet=False)
    m = experiments.commercial_metrics(store, "deep_preflight")
    assert m["paid_decisions"] == 0
    assert m["distinct_external_payers"] == 0
    assert m["external_settled_revenue_usd"] == 0.0
    assert m["supporting_testnet_or_unconfirmed_NOT_REVENUE"] == 1


def test_unconfirmed_mainnet_settlement_does_not_count(store):
    """A facilitator claiming success is not a chain receipt."""
    _settled_event(store, confirmed=False)
    m = experiments.commercial_metrics(store, "deep_preflight")
    assert m["paid_decisions"] == 0
    assert m["supporting_testnet_or_unconfirmed_NOT_REVENUE"] == 1


def test_confirmed_mainnet_external_settlement_counts(store):
    _settled_event(store)
    m = experiments.commercial_metrics(store, "deep_preflight")
    assert m["paid_decisions"] == 1
    assert m["distinct_external_payers"] == 1
    assert m["external_settled_revenue_usd"] == pytest.approx(0.02)


def test_is_revenue_requires_all_three_conditions():
    base = {"settlement_mode": "x402", "settlement_confirmed": True,
            "settlement_mainnet": True}
    assert experiments.is_revenue(base) is True
    for drop in ("settlement_mode", "settlement_confirmed", "settlement_mainnet"):
        bad = {**base, drop: (False if drop != "settlement_mode" else "credits_sandbox")}
        assert experiments.is_revenue(bad) is False, drop


def test_testnet_volume_cannot_promote(store):
    experiments.define(store, "e", hypothesis="h", variable="price:deep_preflight",
                       baseline={m: 0 for m in experiments.PRIMARY_METRICS})
    for i in range(30):
        _settled_event(store, key=f"a2a:net:p{i}", ua=f"a2a:langchain/0.2.{i}",
                       mainnet=False)
    assert experiments.evaluate(store, "e")["decision"] != "promote"


# --------------------------------------------------------------------------
# 3. An experiment must be judged on ITS OWN operation
# --------------------------------------------------------------------------
def test_unrelated_settlement_cannot_promote_a_deep_preflight_experiment(store):
    """The defect: any global primary metric moving promoted any experiment, so
    revenue from a completely different product would 'prove' a price change
    worked."""
    exp = experiments.define(store, "deep", hypothesis="h",
                            variable="price:deep_preflight",
                            baseline={m: 0 for m in experiments.PRIMARY_METRICS})
    exp["min_qualified"] = 1
    store.experiments["deep"] = exp
    # qualified exposure on the deep-preflight surface
    store.record_event("a2a:net:looker", "deep_preflight_run",
                       ua="a2a:langchain/0.2.1", endpoint="preflight_deep")
    # ...and a pile of REAL money from an entirely different operation
    for i in range(20):
        _settled_event(store, etype="evidence_bundle_issued",
                       key=f"a2a:net:other{i}", ua=f"a2a:langchain/0.3.{i}")
    out = experiments.evaluate(store, "deep")
    assert out["decision"] != "promote", out
    assert out["evidence"]["operation"] == "deep_preflight"
    assert out["evidence"]["metrics"]["paid_decisions"] == 0


def test_the_experiments_own_operation_does_promote(store):
    exp = experiments.define(store, "deep", hypothesis="h",
                            variable="price:deep_preflight",
                            baseline={m: 0 for m in experiments.PRIMARY_METRICS})
    exp["min_qualified"] = 1
    store.experiments["deep"] = exp
    _settled_event(store, key="a2a:net:realpayer")
    assert experiments.evaluate(store, "deep")["decision"] == "promote"


def test_exposure_is_scoped_to_the_experiments_surface(store):
    store.record_event("a2a:net:a", "watch_provisioned",
                       ua="a2a:langchain/0.2.1", endpoint="watch")
    deep = experiments.qualified_exposure(store, "deep_preflight")
    watch = experiments.qualified_exposure(store, "watch_cycle")
    assert deep["qualified_actors"] == 0
    assert watch["qualified_actors"] == 1


def test_only_one_change_is_applied_per_cycle_globally(store):
    """Two prices moving in one cycle makes both results uninterpretable."""
    for key, op in (("a", "deep_preflight"), ("b", "evidence_bundle")):
        exp = experiments.define(store, key, hypothesis="h",
                                 variable=f"price:{op}",
                                 baseline={m: 0 for m in experiments.PRIMARY_METRICS})
        exp["min_qualified"] = 1
        store.experiments[key] = exp
    store.record_event("a2a:net:x", "deep_preflight_run",
                       ua="a2a:langchain/0.2.1", endpoint="preflight_deep")
    store.record_event("a2a:net:y", "evidence_bundle_issued",
                       ua="a2a:langchain/0.2.2", endpoint="evidence_bundle")
    applied = experiments.apply_next_action(store)
    acted = [r for r in applied if r.get("acted")]
    assert len(acted) == 1, applied
    deferred = [r for r in applied
                if r.get("reason") == "deferred_one_change_per_cycle"]
    assert len(deferred) == 1


# --------------------------------------------------------------------------
# 4. The A2A card leads with the decision
# --------------------------------------------------------------------------
def test_agent_card_description_leads_with_the_endpoint_decision():
    from fastapi.testclient import TestClient
    from app.main import app
    card = TestClient(app).get("/.well-known/agent-card.json").json()
    desc = card["description"]
    assert desc.lower().startswith("can i safely use or pay this endpoint")
    assert "preflight" in desc.lower()
    # passports may still be mentioned — as supporting, not as the opener
    head = desc[:200].lower()
    assert "passport" not in head
