"""The trust read must be sellable, scopable and REVENUE-VISIBLE end to end.

Defect this pins (found live 2026-08-13, release 2.5.7 / 1c4f98b):

  The x402 gateway has charged for the trust read as operation ``best_agent``
  (GET /check, GET /search, guild_check / guild_search / guild_best_agent, the
  A2A capability ask) since the rail went live — and the ONLY qualified
  external paid-offer challenges in the live funnel named exactly that
  operation. Yet:

    * ``best_agent`` was absent from ``experiments.OPERATION_EVENTS``: the
      challenges surfaced as unattributed RESIDUE
      (``{"best_agent": 2}``), and ``?operation=best_agent`` was a 400;
    * it was absent from ``paidcatalog._OPERATIONS``: a price real buyers
      were being quoted that no catalogue/manifest/registry reader could
      discover;
    * NO transport recorded a completion event for it, so a settled payment
      — including a confirmed external MAINNET settlement — would have been
      invisible to /commercial: real revenue reported as $0.00 forever.
    * ``signed_decision`` (POST /check/decision, GET /check?signed=true) had
      the same catalogue/scope gaps, with the extra twist that its completion
      event was ALREADY recorded and simply not counted.

These tests drive the REAL surfaces (FastAPI routes, the A2A x402 producer)
and then ask the experiment engine what it learned, in the same transport-level
discipline as tests/test_paid_impressions_transport.py.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import experiments, paidcatalog, payments, pricing  # noqa: E402
from app.store import Store  # noqa: E402

EXT_UA = "langchain/0.2.1"


@pytest.fixture()
def store(tmp_path, monkeypatch) -> Store:
    pricing.load_runtime({})
    s = Store(path=str(tmp_path / "guild.json"))
    import app.main as main_mod
    import app.a2a as a2a_mod
    import app.a2a_x402 as a2a_x402_mod
    import app.mcp_server as mcp_mod
    import app.state as state_mod
    monkeypatch.setattr(main_mod, "store", s)
    monkeypatch.setattr(a2a_mod, "store", s)
    monkeypatch.setattr(a2a_x402_mod, "store", s)
    monkeypatch.setattr(mcp_mod, "store", s)
    # demand.record_demand resolves app.state.store at CALL time — without
    # this, every /check call here would write a genuine-external
    # capability_demand row into the GLOBAL store and could push another
    # test's capability out of the scout's bounded per-run demand window
    # (MAX_CAPABILITIES_PER_RUN) later in the suite.
    monkeypatch.setattr(state_mod, "store", s)
    return s


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _served(store: Store, endpoint: str | None = None):
    return [e for e in store.events
            if e.get("type") == "best_agent_served"
            and (endpoint is None or e.get("endpoint") == endpoint)]


# --------------------------------------------------------------------------
# Catalogue / scope parity
# --------------------------------------------------------------------------
def test_best_agent_and_signed_decision_are_measurable_operations():
    assert experiments.OPERATION_EVENTS["best_agent"] == ("best_agent_served",)
    assert experiments.OPERATION_EVENTS["signed_decision"] == (
        "signed_decision_issued",)


def test_scoped_reads_accept_best_agent():
    from app.main import _require_known_operation
    _require_known_operation("best_agent")
    _require_known_operation("signed_decision")


def test_catalogue_advertises_the_gateway_price_not_zero():
    """pricing.price() returns 0 for legacy-priced operations; the catalogue
    must advertise what the gateway actually charges (PaidRequest.cost)."""
    ops = {o["operation"]: o for o in paidcatalog.operations()}
    for name in ("best_agent", "signed_decision"):
        assert name in ops, f"{name} is charged at the gateway but not advertised"
        preq = paidcatalog._settlement_request(name)
        assert preq.cost > 0, (
            f"{name}: a $0 gateway price would make this entry untruthful")
        assert ops[name]["price_credits"] == preq.cost
        assert ops[name]["price_usd"] != "$0", (
            f"{name}: advertised $0 for a request the gateway bills")


def test_settlement_binding_matches_the_charging_builder():
    ops = {o["operation"]: o for o in paidcatalog.operations()}
    ba = paidcatalog._settlement_request("best_agent")
    assert ba.operation == "best_agent"
    assert ops["best_agent"]["settlement"]["canonical_resource"] == \
        ba.resource_url
    sd = paidcatalog._settlement_request("signed_decision")
    assert sd.operation == "signed_decision"


# --------------------------------------------------------------------------
# Completion events through the real HTTP surfaces
# --------------------------------------------------------------------------
def test_http_check_records_a_completion_with_settlement_facts(store, client):
    r = client.get("/check", params={"capability": "fact-check"},
                   headers={"user-agent": EXT_UA})
    assert r.status_code == 200, r.text
    served = _served(store, "check")
    assert len(served) == 1
    e = served[0]
    assert e["capability"] == "fact-check"
    assert e["price_credits"] == payments.check_request("fact-check").cost
    # Soft-launch free call: facts must SAY so — free is never counted as a
    # paid completion, but the field has to exist to be judged.
    assert e["settlement_mode"] == "free"
    assert e["paid"] is False


def test_http_search_records_a_completion(store, client):
    r = client.get("/search", params={"capability": "fact-check"},
                   headers={"user-agent": EXT_UA})
    assert r.status_code == 200, r.text
    assert len(_served(store, "search")) == 1


def test_http_signed_check_records_signed_decision_issued(store, client):
    r = client.get("/check", params={"capability": "fact-check",
                                     "signed": "true"},
                   headers={"user-agent": EXT_UA})
    assert r.status_code == 200, r.text
    issued = [e for e in store.events
              if e.get("type") == "signed_decision_issued"
              and e.get("endpoint") == "check_signed"]
    assert len(issued) == 1
    assert issued[0]["settlement_mode"] == "free"


# --------------------------------------------------------------------------
# The A2A settled producer records the completion
# --------------------------------------------------------------------------
def test_a2a_settled_best_agent_records_completion_with_facts(store):
    from app import a2a_x402

    class _Settled:
        record = {"confirmed": True, "mainnet": False,
                  "network": "base-sepolia", "amount_atomic": 10000,
                  "transaction": "0x" + "ab" * 32,
                  "payer_attribution": "unverified_payer"}

    preq = payments.check_request("fact-check")
    task = {"operation": "best_agent",
            "operation_params": {"capability": "fact-check"},
            "credits_cost": preq.cost,
            "actor": "a2a:net:deadbeef", "ua": EXT_UA}
    out = a2a_x402._produce_for(preq, _Settled(), task)
    assert "shortlist" in out or "best_agent" in out
    served = _served(store)
    assert len(served) == 1
    e = served[0]
    assert e["transport"] == "a2a"
    assert e["settlement_mode"] == "x402"
    assert e["settlement_confirmed"] is True
    assert e["settlement_mainnet"] is False  # sepolia is not money
    assert e["price_credits"] == preq.cost


# --------------------------------------------------------------------------
# Revenue visibility: the exact three-condition rule, now reachable
# --------------------------------------------------------------------------
def _completion(store: Store, *, mainnet: bool, confirmed: bool = True,
                key: str = "http:payer1",
                attribution: str =
                "independently_attested_external_machine") -> None:
    store.record_event(key, "best_agent_served", ua=EXT_UA,
                       endpoint="check", transport="http",
                       capability="fact-check", price_credits=10, paid=True,
                       settlement_mode="x402", settlement_confirmed=confirmed,
                       settlement_mainnet=mainnet,
                       settlement_network=("base" if mainnet
                                           else "base-sepolia"),
                       settlement_amount_atomic=10000,
                       settlement_tx="0x" + "cd" * 32,
                       payer_attribution=attribution)


def test_confirmed_mainnet_best_agent_settlement_is_revenue(store):
    _completion(store, mainnet=True)
    m = experiments.commercial_metrics(store, operation="best_agent")
    assert m["external_settled_revenue_usd"] == pytest.approx(0.01)
    assert m["paid_decisions"] == 1
    # ... and it rolls up into the unscoped total the same way.
    all_m = experiments.commercial_metrics(store)
    assert all_m["external_settled_revenue_usd"] == pytest.approx(0.01)


def test_testnet_best_agent_settlement_is_still_not_revenue(store):
    """Widening OPERATION_EVENTS must not weaken the three-condition rule."""
    _completion(store, mainnet=False)
    m = experiments.commercial_metrics(store, operation="best_agent")
    assert m["external_settled_revenue_usd"] == 0.0
    assert m["supporting_testnet_or_unconfirmed_NOT_REVENUE"] == 1


@pytest.mark.parametrize("attribution", [
    "unverified_payer",
    "cryptographically_bound_machine_payer",
    "verified_first_party_canary",
])
def test_mainnet_without_independent_externality_is_not_revenue(
        store, attribution):
    _completion(store, mainnet=True, attribution=attribution)
    m = experiments.commercial_metrics(store, operation="best_agent")
    assert m["external_settled_revenue_usd"] == 0.0
    assert m["paid_decisions"] == 0
    assert m["settled_but_not_attributable_external"] == 1


def test_a2a_settled_signed_decision_records_completion(store):
    from app import a2a_x402

    class _Settled:
        record = {"confirmed": True, "mainnet": False,
                  "network": "eip155:84532", "amount_atomic": 1000000,
                  "transaction": "0x" + "ef" * 32,
                  "payer_attribution": "unverified_payer"}

    preq = payments.check_request("fact-check", signed=True,
                                  ttl_seconds=600)
    task = {"operation": "signed_decision",
            "operation_params": {"capability": "fact-check",
                                 "signed": True, "ttl_seconds": 600},
            "credits_cost": preq.cost,
            "actor": "a2a:net:feed01", "ua": EXT_UA}
    out = a2a_x402._produce_for(preq, _Settled(), task)
    assert out["type"] == "AgentGuildDecision"
    issued = [e for e in store.events
              if e.get("type") == "signed_decision_issued"]
    assert len(issued) == 1
    assert issued[0]["transport"] == "a2a"
    assert issued[0]["settlement_mainnet"] is False


# --------------------------------------------------------------------------
# The live residue reconciles: challenges naming best_agent are attributable
# --------------------------------------------------------------------------
def test_best_agent_challenges_leave_the_residue(store):
    store.record_event("a2a:net:cafe01", "paid_offer_shown", ua=EXT_UA,
                       endpoint="x402_challenge", transport="a2a",
                       challenged_operation="best_agent",
                       impression="challenge_402", actor_distinct=True,
                       price_credits=10)
    scoped = experiments.qualified_exposure(store, operation="best_agent")
    assert scoped["paid_offers_shown"] == 1
    portfolio = experiments.qualified_exposure(store)
    assert portfolio["paid_offers_shown"] == 1
    assert portfolio["unattributed_paid_offers_shown"] == 0
    assert portfolio["unattributed_paid_offer_operations"] == {}
