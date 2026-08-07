"""Paid-offer impressions through the REAL transports.

The previous round defined the impression event correctly and then failed to
populate it on any real path — the events existed, the counters moved, and the
number the engine gates on (distinct ACTORS) stayed at zero. Hand-constructed
event tests could not see that, because they wrote the events themselves.

So every test here drives an actual request through FastAPI, the mounted MCP
tool, or the A2A JSON-RPC endpoint, and then asks the experiment engine what it
learned. Three specific regressions are locked:

  * HTTP recorded `actor=None`, so a genuine external challenge never
    incremented qualified_actors;
  * unauthenticated MCP callers collapsed into the literal actor "mcp", so a
    thousand distinct clients counted as one;
  * a watch has no 402 at all, so its price could never be observed as offered
    to anyone and a watch pricing experiment could never become decidable.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import experiments, pricing  # noqa: E402
from app.store import Store  # noqa: E402

EXT_UA = "langchain/0.2.1"


@pytest.fixture()
def store(tmp_path, monkeypatch) -> Store:
    pricing.load_runtime({})
    s = Store(path=str(tmp_path / "guild.json"))
    import app.main as main_mod
    import app.a2a as a2a_mod
    import app.mcp_server as mcp_mod
    monkeypatch.setattr(main_mod, "store", s)
    monkeypatch.setattr(a2a_mod, "store", s)
    monkeypatch.setattr(mcp_mod, "store", s)
    return s


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


@pytest.fixture()
def enforced(monkeypatch):
    """Turn on billing enforcement so priced reads actually challenge."""
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")


def _impressions(store: Store, operation: str):
    return [e for e in store.events
            if e.get("type") == "paid_offer_shown"
            and e.get("challenged_operation") == operation]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def test_http_challenge_carries_a_real_distinct_actor(store, client, enforced):
    """The defect: actor=None, so events moved and qualified_actors never did."""
    r = client.get("/preflight/deep", params={"url": "https://x.example/a2a"},
                   headers={"user-agent": EXT_UA})
    assert r.status_code == 402, r.status_code
    shown = _impressions(store, "deep_preflight")
    assert len(shown) == 1
    actor = shown[0]["key"]
    assert actor and actor != "anon", "the challenge must be attributable"
    assert actor.startswith("http:")
    assert "langchain" not in actor, "the actor must not embed raw caller data"


def test_http_challenges_from_different_callers_are_different_actors(
        store, client, enforced):
    for ua in ("langchain/0.2.1", "crewai/1.0", "llamaindex/0.9"):
        client.get("/preflight/deep", params={"url": "https://x.example/a2a"},
                   headers={"user-agent": ua})
    actors = {e["key"] for e in _impressions(store, "deep_preflight")}
    assert len(actors) == 3, actors


def test_http_free_preflight_records_no_paid_impression(store, client):
    r = client.get("/preflight", params={"url": "https://x.example/a2a"},
                   headers={"user-agent": EXT_UA})
    assert r.status_code == 200
    assert _impressions(store, "deep_preflight") == []


def test_free_preflight_alone_cannot_make_deep_pricing_decisive_over_http(
        store, client):
    e = experiments.define(store, "deep", hypothesis="h",
                           variable="price:deep_preflight",
                           baseline={m: 0 for m in experiments.PRIMARY_METRICS})
    e["min_qualified"] = 2
    store.experiments["deep"] = e
    for i in range(30):
        client.get("/preflight", params={"url": f"https://x{i}.example/a2a"},
                   headers={"user-agent": f"langchain/0.2.{i}"})
    assert experiments.qualified_exposure(
        store, "deep_preflight")["qualified_actors"] == 0
    before = pricing.price("deep_preflight")
    experiments.apply_next_action(store)
    assert pricing.price("deep_preflight") == before


def test_genuine_http_challenges_make_deep_pricing_decidable(
        store, client, enforced):
    e = experiments.define(store, "deep", hypothesis="h",
                           variable="price:deep_preflight",
                           baseline={m: 0 for m in experiments.PRIMARY_METRICS})
    e["min_qualified"] = 2
    store.experiments["deep"] = e
    for ua in ("langchain/0.2.1", "crewai/1.0"):
        client.get("/preflight/deep", params={"url": "https://x.example/a2a"},
                   headers={"user-agent": ua})
    exp = experiments.qualified_exposure(store, "deep_preflight")
    assert exp["qualified_actors"] == 2, exp
    assert experiments.evaluate(store, "deep")["decision"] == "kill"


# --------------------------------------------------------------------------
# Watch — priced, but never behind a 402
# --------------------------------------------------------------------------
def _funded(store: Store, name="w"):
    rec = store.register_agent(name, ["x"], {})
    raw = rec.get("api_key")
    store.credit(store._account_key(raw), 1000, reason="test")
    return raw


def test_http_watch_response_records_the_price_impression(store, client):
    raw = _funded(store)
    r = client.post("/watch", json={"url": "https://a.example/a2a"},
                    headers={"x-api-key": raw, "user-agent": EXT_UA})
    assert r.status_code == 200
    assert r.json()["price_per_cycle_credits"] == pricing.price("watch_cycle")
    shown = _impressions(store, "watch_cycle")
    assert len(shown) == 1
    assert shown[0]["impression"] == "price_displayed"
    assert shown[0]["price_credits"] == pricing.price("watch_cycle")


def test_a_price_impression_is_never_recorded_as_a_payment(store, client):
    raw = _funded(store)
    client.post("/watch", json={"url": "https://a.example/a2a"},
                headers={"x-api-key": raw, "user-agent": EXT_UA})
    shown = _impressions(store, "watch_cycle")[0]
    assert not shown.get("paid")
    assert shown.get("settlement_mode") is None
    m = experiments.commercial_metrics(store, "watch_cycle")
    assert m["paid_decisions"] == 0, "provisioning is free and must stay free"


def test_a_watch_experiment_can_become_decidable(store, client):
    """Previously impossible: no 402 meant no impression, ever."""
    e = experiments.define(store, "w", hypothesis="h",
                           variable="price:watch_cycle",
                           baseline={m: 0 for m in experiments.PRIMARY_METRICS})
    e["min_qualified"] = 2
    store.experiments["w"] = e
    for i in range(2):
        raw = _funded(store, f"w{i}")
        client.post("/watch", json={"url": f"https://a{i}.example/a2a"},
                    headers={"x-api-key": raw, "user-agent": f"langchain/0.2.{i}"})
    exp = experiments.qualified_exposure(store, "watch_cycle")
    assert exp["qualified_actors"] == 2, exp
    assert experiments.evaluate(store, "w")["decision"] in ("kill", "promote")


# --------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------
def _tool(name):
    import app.mcp_server as m
    tool = getattr(m, name)
    for attr in ("fn", "func", "__wrapped__"):
        f = getattr(tool, attr, None)
        if callable(f):
            return f
    return tool


class _Ctx:
    """Minimal stand-in for the MCP Context clientInfo handshake."""

    def __init__(self, name, version="1.0"):
        class _CI:
            pass
        ci = _CI()
        ci.name, ci.version = name, version

        class _P:
            clientInfo = ci

        class _S:
            client_params = _P()
        self.session = _S()


def test_unauthenticated_mcp_callers_are_not_all_one_actor(store, monkeypatch):
    """The defect: every unauthenticated caller collapsed into literal "mcp"."""
    import app.mcp_server as m
    a, a_distinct = m._mcp_actor(_Ctx("alpha-client"))
    b, b_distinct = m._mcp_actor(_Ctx("beta-client"))
    assert a != b
    assert a_distinct and b_distinct
    assert a.startswith("mcp:net:")
    assert "alpha-client" not in a, "the actor must not embed the raw client id"


def test_the_same_mcp_client_is_a_stable_actor(store):
    import app.mcp_server as m
    assert m._mcp_actor(_Ctx("same"))[0] == m._mcp_actor(_Ctx("same"))[0]


def test_an_unidentifiable_mcp_caller_is_explicitly_not_distinct(store):
    """We do not invent identities. When distinctness is unknowable we say so,
    and the caller does not count toward an actor threshold."""
    import app.mcp_server as m
    actor, distinct = m._mcp_actor(None)
    assert distinct is False
    assert actor == "mcp:unidentified"


def test_indistinct_mcp_impressions_do_not_reach_the_threshold(store):
    for _ in range(50):
        store.record_event("mcp:unidentified", "paid_offer_shown",
                           ua="mcp/remote", endpoint="x402_challenge",
                           challenged_operation="deep_preflight",
                           impression="challenge_402", actor_distinct=False)
    assert experiments.qualified_exposure(
        store, "deep_preflight")["qualified_actors"] == 0


def test_mcp_watch_tool_records_the_price_impression(store):
    raw = _funded(store, "mcpw")
    out = _tool("guild_watch")(url="https://a.example/a2a", api_key=raw,
                               ctx=_Ctx("langchain"))
    assert out["price_per_cycle_credits"] == pricing.price("watch_cycle")
    assert len(_impressions(store, "watch_cycle")) == 1


# --------------------------------------------------------------------------
# A2A
# --------------------------------------------------------------------------
def _a2a(client, text, ua=EXT_UA):
    return client.post("/a2a", headers={"user-agent": ua}, json={
        "jsonrpc": "2.0", "id": "1", "method": "message/send",
        "params": {"message": {"role": "user",
                               "parts": [{"kind": "text", "text": text}]}}})


def test_a2a_free_preflight_records_no_paid_impression(store, client, monkeypatch):
    import app.a2a as a2a_mod
    monkeypatch.setattr(a2a_mod.preflight, "run",
                        lambda url, store=None: {"verdict": "no_failed_checks",
                                                 "failed": [], "unknowns": [],
                                                 "checks": []})
    r = _a2a(client, "preflight: https://x.example/a2a")
    assert r.status_code == 200
    assert _impressions(store, "deep_preflight") == []


def test_a2a_deep_preflight_challenge_records_a_distinct_actor(
        store, client, monkeypatch):
    import app.a2a as a2a_mod
    monkeypatch.setattr(a2a_mod, "_x402_a2a_active", lambda: True)
    r = _a2a(client, "deep-preflight: https://x.example/a2a")
    assert r.status_code == 200
    shown = _impressions(store, "deep_preflight")
    assert len(shown) == 1
    assert shown[0]["key"].startswith("a2a:")
    assert shown[0]["impression"] == "challenge_402"


# --------------------------------------------------------------------------
# Seeding — an engine with no experiment is inert
# --------------------------------------------------------------------------
def test_a_fresh_store_cycle_seeds_the_bounded_operation_experiments(store, monkeypatch):
    """Deployed 061dcea returned experiments: {} — the engine had nothing to
    learn from and could never satisfy 'find the formula without a human'."""
    from app.swarm import runner
    monkeypatch.setenv("GUILD_INDEX_AUTORUN", "1")
    monkeypatch.setattr(runner, "_run_watch_cycles", lambda s, cap=10: {})
    monkeypatch.setattr("app.indexops.recheck_due",
                        lambda s, **kw: {"checked": 0})
    monkeypatch.setattr("app.indexops.ingest", lambda s, r=None: {"added": 0})
    assert store.experiments == {}
    out = runner._run_index_cycle(store)
    assert out["seeded"]["seeded"] == [
        "machine_envelope_price_v1", "deep_preflight_price_v1"]
    env = store.experiments["machine_envelope_price_v1"]
    assert env["variable"] == "price:machine_envelope"
    assert env["tested_price_credits"] == pricing.price("machine_envelope")
    assert len(store.experiments) == 2
    rec = store.experiments["deep_preflight_price_v1"]
    assert rec["variable"] == "price:deep_preflight"
    assert rec["baseline"]["operation_scope"] == "deep_preflight"


def test_seeding_is_idempotent_and_never_resets_the_window(store, monkeypatch):
    """A seeder that runs every cycle is one bug away from continuously
    resetting the thing it is meant to measure."""
    experiments.seed_defaults(store)
    rec = dict(store.experiments["deep_preflight_price_v1"])
    started, baseline = rec["started_at"], rec["baseline"]
    for _ in range(3):
        out = experiments.seed_defaults(store)
        assert out["seeded"] == []
    live = store.experiments["deep_preflight_price_v1"]
    assert live["started_at"] == started
    assert live["baseline"] == baseline
    assert len(store.experiments) == 2


def test_a_restart_does_not_reset_the_experiment(store, tmp_path):
    experiments.seed_defaults(store)
    started = store.experiments["deep_preflight_price_v1"]["started_at"]
    reloaded = Store(path=str(tmp_path / "guild.json"))
    experiments.seed_defaults(reloaded)
    assert reloaded.experiments["deep_preflight_price_v1"]["started_at"] == started


def test_an_operator_pinned_operation_is_not_seeded(store, monkeypatch):
    monkeypatch.setenv("GUILD_PRICE_DEEP_PREFLIGHT", "20")
    out = experiments.seed_defaults(store)
    assert out["seeded"] == ["machine_envelope_price_v1"]
    skipped = {row["key"]: row["reason"] for row in out["already_present"]}
    assert skipped["deep_preflight_price_v1"] == "price_pinned_by_operator"


def test_a_seeded_experiment_without_exposure_is_not_decisive(store):
    """Seeding must not fabricate exposure: the honest state of a product
    nobody has been offered is insufficient_evidence, not a verdict."""
    experiments.seed_defaults(store)
    out = experiments.evaluate(store, "deep_preflight_price_v1")
    assert out["decision"] in ("hold", "insufficient_evidence")
    before = pricing.price("deep_preflight")
    experiments.apply_next_action(store)
    assert pricing.price("deep_preflight") == before


# --------------------------------------------------------------------------
# Treatment window + exact treatment — the stale-evidence defect
# --------------------------------------------------------------------------
def _impression(store: Store, actor: str, operation: str, price: int,
                at: str = None):
    store.record_event(actor, "paid_offer_shown", ua="langchain/0.2.1",
                       endpoint="x402_challenge",
                       challenged_operation=operation,
                       impression="challenge_402", price_credits=price)
    if at:
        store.events[-1]["at"] = at


def test_old_price_impressions_cannot_decide_the_new_price_window(store):
    """THE defect: ten callers see 20 credits and do not buy, the engine cuts
    to 10, and the next cycle reuses those same ten impressions to justify
    cutting 10 to 5 — even though nobody has been shown 10."""
    e = experiments.define(store, "deep", hypothesis="h",
                           variable="price:deep_preflight",
                           baseline={m: 0 for m in experiments.PRIMARY_METRICS},
                           tested_price_credits=20)
    e["min_qualified"] = 2
    store.experiments["deep"] = e
    for i in range(4):
        _impression(store, f"a2a:net:old{i}", "deep_preflight", 20)

    first = experiments.apply_next_action(store)
    assert first[0]["acted"] is True
    new_price = pricing.price("deep_preflight")
    assert new_price == 10

    # The SAME old-price impressions must not decide the new arm.
    exposure = experiments.qualified_exposure(
        store, "deep_preflight",
        since=store.experiments["deep"]["started_at"],
        tested_price_credits=new_price)
    assert exposure["qualified_actors"] == 0, exposure
    second = experiments.apply_next_action(store)
    assert second[0].get("acted") is not True
    assert pricing.price("deep_preflight") == new_price, \
        "a second cut on stale evidence is exactly the defect"


def test_impressions_of_the_new_price_do_decide_the_new_window(store):
    e = experiments.define(store, "deep", hypothesis="h",
                           variable="price:deep_preflight",
                           baseline={m: 0 for m in experiments.PRIMARY_METRICS},
                           tested_price_credits=20)
    e["min_qualified"] = 2
    store.experiments["deep"] = e
    for i in range(2):
        _impression(store, f"a2a:net:old{i}", "deep_preflight", 20)
    experiments.apply_next_action(store)
    assert pricing.price("deep_preflight") == 10
    # now two callers actually see 10
    for i in range(2):
        _impression(store, f"a2a:net:new{i}", "deep_preflight", 10)
    out = experiments.apply_next_action(store)
    assert out[0]["acted"] is True
    assert pricing.price("deep_preflight") == 5


def test_an_impression_from_before_the_window_is_excluded(store):
    experiments.define(store, "deep", hypothesis="h",
                       variable="price:deep_preflight",
                       baseline={m: 0 for m in experiments.PRIMARY_METRICS},
                       tested_price_credits=20)
    _impression(store, "a2a:net:ancient", "deep_preflight", 20,
                at="2020-01-01T00:00:00+00:00")
    exposure = experiments.qualified_exposure(
        store, "deep_preflight",
        since=store.experiments["deep"]["started_at"],
        tested_price_credits=20)
    assert exposure["qualified_actors"] == 0


def test_unparseable_event_times_fail_closed(store):
    experiments.define(store, "deep", hypothesis="h",
                       variable="price:deep_preflight",
                       baseline={m: 0 for m in experiments.PRIMARY_METRICS},
                       tested_price_credits=20)
    _impression(store, "a2a:net:bad", "deep_preflight", 20, at="not-a-date")
    exposure = experiments.qualified_exposure(
        store, "deep_preflight",
        since=store.experiments["deep"]["started_at"],
        tested_price_credits=20)
    assert exposure["qualified_actors"] == 0, \
        "an unreadable timestamp must never be able to decide a price"


def test_a_payment_quoted_at_the_old_price_cannot_promote_the_new_one(store):
    """The money is real; it is simply evidence about the price the payer was
    actually shown."""
    experiments.define(store, "deep", hypothesis="h",
                       variable="price:deep_preflight",
                       baseline={m: 0 for m in experiments.PRIMARY_METRICS},
                       tested_price_credits=10)
    store.record_event("a2a:net:payer", "deep_preflight_run",
                       ua="langchain/0.2.1", endpoint="preflight_deep",
                       price_credits=20,          # quoted under the OLD arm
                       settlement_mode="x402", settlement_confirmed=True,
                       settlement_mainnet=True, settlement_amount_atomic=20000)
    m = experiments.commercial_metrics(
        store, "deep_preflight",
        since=store.experiments["deep"]["started_at"],
        tested_price_credits=10)
    assert m["paid_decisions"] == 0
    assert m["external_settled_revenue_usd"] == 0.0


def test_the_portfolio_report_is_not_scoped_away(store):
    """Scoping applies to EXPERIMENT EVALUATION only — the commercial report
    must still show everything that happened."""
    store.record_event("a2a:net:payer", "deep_preflight_run",
                       ua="langchain/0.2.1", endpoint="preflight_deep",
                       price_credits=20, settlement_mode="x402",
                       settlement_confirmed=True, settlement_mainnet=True,
                       settlement_amount_atomic=20000)
    assert experiments.commercial_metrics(store)["paid_decisions"] == 1
    assert experiments.commercial_metrics(
        store, "deep_preflight")["paid_decisions"] == 1


def test_reprice_updates_window_and_treatment_together(store):
    e = experiments.define(store, "deep", hypothesis="h",
                           variable="price:deep_preflight",
                           baseline={m: 0 for m in experiments.PRIMARY_METRICS},
                           tested_price_credits=20)
    e["min_qualified"] = 1
    store.experiments["deep"] = e
    started = e["started_at"]
    _impression(store, "a2a:net:one", "deep_preflight", 20)
    experiments.apply_next_action(store)
    live = store.experiments["deep"]
    assert live["tested_price_credits"] == 10
    assert live["started_at"] >= started
    assert live["baseline"]["operation_scope"] == "deep_preflight"


def test_a_completion_with_no_recorded_price_is_excluded(store):
    """Fail closed: a payment we cannot place in this arm must not promote it.
    Excluding a real sale understates us; admitting an unattributable one lets
    any historical payment promote a price it was never quoted at."""
    experiments.define(store, "deep", hypothesis="h",
                       variable="price:deep_preflight",
                       baseline={m: 0 for m in experiments.PRIMARY_METRICS},
                       tested_price_credits=10)
    store.record_event("a2a:net:payer", "deep_preflight_run",
                       ua="langchain/0.2.1", endpoint="preflight_deep",
                       settlement_mode="x402", settlement_confirmed=True,
                       settlement_mainnet=True, settlement_amount_atomic=20000)
    m = experiments.commercial_metrics(
        store, "deep_preflight",
        since=store.experiments["deep"]["started_at"],
        tested_price_credits=10)
    assert m["paid_decisions"] == 0
    assert m["external_settled_revenue_usd"] == 0.0


def test_historical_revenue_cannot_suppress_a_new_arm_sale(store):
    """Seed baseline must be in the SAME frame as the comparison, or a real
    new-arm sale reads as 'no movement' against all-time history."""
    store.record_event("a2a:net:old", "deep_preflight_run",
                       ua="langchain/0.2.1", endpoint="preflight_deep",
                       price_credits=20, settlement_mode="x402",
                       settlement_confirmed=True, settlement_mainnet=True,
                       settlement_amount_atomic=99000)
    experiments.seed_defaults(store)
    rec = store.experiments["deep_preflight_price_v1"]
    assert rec["baseline"]["paid_decisions"] == 0, rec["baseline"]
    assert rec["baseline"]["external_settled_revenue_usd"] == 0.0

    rec["min_qualified"] = 1
    store.experiments["deep_preflight_price_v1"] = rec
    store.record_event("a2a:net:new", "deep_preflight_run",
                       ua="crewai/1.0", endpoint="preflight_deep",
                       price_credits=rec["tested_price_credits"],
                       settlement_mode="x402", settlement_confirmed=True,
                       settlement_mainnet=True, settlement_amount_atomic=20000)
    out = experiments.evaluate(store, "deep_preflight_price_v1")
    assert out["decision"] == "promote", out["evidence"]
