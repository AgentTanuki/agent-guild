"""Corrections to the trust-index release — review findings, 2026-07-31.

Six release-blocking gaps were found by independent review of 6b361e6. Each one
had the same shape: the code looked right and the tests passed, but the SYSTEM
could not actually do what the docstring claimed, or could be made to lie.

  1. the engine could decide but not ACT — a strategy memo with a cron job
  2. `paid=True` meant "passed the gate", so our own trial credits could
     promote an experiment
  3. a watch could be provisioned by anyone with any string, scheduling
     outbound work we could never bill
  4. the evidence page interpolated registry-controlled strings into HTML
  5. no A2A decision surface at all
  6. the landing page still sold the previous product

These tests are the ones that would have caught them.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import experiments, indexops, pricing, trustindex  # noqa: E402
from app.store import Store  # noqa: E402


@pytest.fixture()
def store(tmp_path) -> Store:
    pricing.load_runtime({})          # never leak overrides between tests
    return Store(path=str(tmp_path / "guild.json"))


def _observed(verdict="no_failed_checks", failed=(), unknowns=()):
    return {"verdict": verdict, "failed": list(failed),
            "unknowns": list(unknowns),
            "checks": [{"check": "endpoint_reachable", "status": "proven",
                        "detail": ""},
                       {"check": "protocol_handshake", "status": "proven",
                        "detail": ""}]}


def _decisive_kill(store: Store, key="exp-kill"):
    exp = experiments.define(
        store, key, hypothesis="price is the blocker",
        variable="price:deep_preflight",
        baseline={m: 0 for m in experiments.PRIMARY_METRICS})
    exp["min_qualified"] = 1
    store.experiments[key] = exp
    # one genuinely external actor reached a decision surface and did not buy
    store.record_event("a2a:net:realcaller", "deep_preflight_run",
                       ua="a2a:langchain/0.2.1", endpoint="preflight_deep")
    return key


# --------------------------------------------------------------------------
# 1. The engine must ACT, durably
# --------------------------------------------------------------------------
def test_a_decisive_kill_actually_changes_the_price(store):
    """Previously the loop called evaluate() only, so a kill produced a
    recommendation nobody read."""
    key = _decisive_kill(store)
    before = pricing.price("deep_preflight")
    applied = experiments.apply_next_action(store)
    row = next(r for r in applied if r["key"] == key)
    assert row["acted"] is True
    assert row["after_credits"] < row["before_credits"] == before
    assert pricing.price("deep_preflight") == row["after_credits"]


def test_the_applied_change_is_durable(store, tmp_path):
    key = _decisive_kill(store)
    experiments.apply_next_action(store)
    changed = pricing.price("deep_preflight")
    assert store.price_overrides["deep_preflight"] == changed
    # a fresh Store over the same file re-installs it
    pricing.load_runtime({})
    reloaded = Store(path=str(tmp_path / "guild.json"))
    assert reloaded.price_overrides.get("deep_preflight") == changed
    assert pricing.price("deep_preflight") == changed


def test_before_after_and_reason_are_recorded(store):
    key = _decisive_kill(store)
    experiments.apply_next_action(store)
    changes = store.experiments[key]["changes_applied"]
    assert changes, "an applied change must be recorded"
    c = changes[-1]
    assert c["before_credits"] > c["after_credits"]
    assert c["reason"] and c["decision"] == "kill"
    assert c["reversible_via"] == "GUILD_PRICE_DEEP_PREFLIGHT"


def test_the_measurement_window_restarts_after_a_change(store):
    key = _decisive_kill(store)
    started_before = store.experiments[key]["started_at"]
    experiments.apply_next_action(store)
    live = store.experiments[key]
    assert live["started_at"] >= started_before
    assert live["decision"] is None, "a changed offer must be re-measured"
    assert live["status"] == "running"
    assert live["baseline"], "a fresh baseline must be taken"


def test_the_engine_can_only_move_a_price_downward(store):
    key = _decisive_kill(store)
    for _ in range(6):
        experiments.apply_next_action(store)
        store.record_event("a2a:net:realcaller", "deep_preflight_run",
                           ua="a2a:langchain/0.2.1", endpoint="preflight_deep")
    assert pricing.price("deep_preflight") <= pricing.DEFAULTS["deep_preflight"]


def test_an_exhausted_offer_is_reported_not_faked(store):
    key = _decisive_kill(store)
    store.price_overrides["deep_preflight"] = 0
    pricing.load_runtime(store.price_overrides)
    applied = experiments.apply_next_action(store)
    row = next(r for r in applied if r["key"] == key)
    assert row["acted"] is False
    assert row["reason"] == "offer_exhausted"


def test_an_operator_pinned_price_is_never_overridden(store, monkeypatch):
    """The human kill switch must outrank the autonomous loop."""
    monkeypatch.setenv("GUILD_PRICE_DEEP_PREFLIGHT", "33")
    key = _decisive_kill(store)
    applied = experiments.apply_next_action(store)
    row = next(r for r in applied if r["key"] == key)
    assert row["acted"] is False
    assert row["reason"] == "price_pinned_by_operator"
    assert pricing.price("deep_preflight") == 33


def test_runtime_overrides_are_reclamped_on_load():
    pricing.load_runtime({"deep_preflight": 10 ** 9, "not_an_operation": 5})
    assert pricing.price("deep_preflight") == pricing.CEILINGS["deep_preflight"]
    assert "not_an_operation" not in pricing.runtime_overrides()
    pricing.load_runtime({})


def test_insufficient_evidence_never_changes_the_price(store):
    experiments.define(store, "exp-quiet", hypothesis="h",
                       variable="price:deep_preflight",
                       baseline={m: 0 for m in experiments.PRIMARY_METRICS})
    before = pricing.price("deep_preflight")
    applied = experiments.apply_next_action(store)
    assert all(not r.get("acted") for r in applied)
    assert pricing.price("deep_preflight") == before


# --------------------------------------------------------------------------
# 2. Settlement mode, not `paid=True`
# --------------------------------------------------------------------------
def test_sandbox_credits_are_not_a_paying_customer(store):
    """The headline defect: our own trial grant could promote an experiment."""
    for i in range(5):
        store.record_event(f"a2a:net:trial{i}", "deep_preflight_run",
                           ua=f"a2a:langchain/0.2.{i}", endpoint="preflight_deep",
                           paid=True, settlement_mode="credits_sandbox")
    m = experiments.commercial_metrics(store)
    assert m["paid_decisions"] == 0
    assert m["distinct_external_payers"] == 0
    assert m["supporting_sandbox_decisions_NOT_REVENUE"] == 5


def test_free_soft_launch_calls_are_not_paid_decisions(store):
    store.record_event("a2a:net:free", "deep_preflight_run",
                       ua="a2a:langchain/0.2.1", endpoint="preflight_deep",
                       paid=False, settlement_mode="free")
    assert experiments.commercial_metrics(store)["paid_decisions"] == 0


def test_legacy_unlabelled_events_count_as_sandbox_never_settled(store):
    """Events recorded before the correction carry no settlement_mode. The
    conservative direction is the only acceptable one."""
    store.record_event("a2a:net:old", "deep_preflight_run",
                       ua="a2a:langchain/0.2.1", endpoint="preflight_deep", paid=True)
    m = experiments.commercial_metrics(store)
    assert m["paid_decisions"] == 0
    assert m["supporting_sandbox_decisions_NOT_REVENUE"] == 1


def test_mainnet_settlement_from_an_external_caller_counts(store):
    store.record_event("a2a:net:payer1", "deep_preflight_run",
                       ua="a2a:langchain/0.2.1", endpoint="preflight_deep",
                       paid=True, settlement_mode="x402")
    m = experiments.commercial_metrics(store)
    assert m["paid_decisions"] == 1
    assert m["distinct_external_payers"] == 1


def test_settled_but_first_party_is_not_a_customer(store):
    store.record_event("ag-internal", "deep_preflight_run",
                       ua="guild-release-gate", endpoint="preflight_deep",
                       paid=True, settlement_mode="x402", first_party=True)
    m = experiments.commercial_metrics(store)
    assert m["paid_decisions"] == 0
    assert m["settled_but_not_attributable_external"] >= 1


def test_sandbox_volume_can_never_promote_an_experiment(store):
    experiments.define(store, "exp-s", hypothesis="h",
                       variable="price:deep_preflight",
                       baseline={m: 0 for m in experiments.PRIMARY_METRICS})
    for i in range(50):
        store.record_event(f"a2a:net:t{i}", "deep_preflight_run",
                           ua=f"a2a:langchain/0.2.{i}", endpoint="preflight_deep",
                           paid=True, settlement_mode="credits_sandbox")
    assert experiments.evaluate(store, "exp-s")["decision"] != "promote"


# --------------------------------------------------------------------------
# 3. A watch must be authenticated and billable BEFORE work is scheduled
# --------------------------------------------------------------------------
def _funded_account(store: Store, credits: int = 500):
    """Register and fund an account, returning (record, RAW key, account key)."""
    rec = store.register_agent("watcher", ["x"], {})
    key = rec.get("api_key")
    acct_key = store._account_key(key)
    store.credit(acct_key, credits, reason="test")
    return rec, key, acct_key


def test_a_bogus_key_cannot_create_a_watch(store):
    with pytest.raises(indexops.UnbillableWatch):
        indexops.provision_watch(store, "totally-made-up", "https://a.example/a2a")
    assert store.watches == {}, "no outbound work may be scheduled"


def test_an_empty_key_cannot_create_a_watch(store):
    for bogus in ("", None, "sk_does_not_exist"):
        with pytest.raises((indexops.UnbillableWatch, ValueError)):
            indexops.provision_watch(store, bogus, "https://a.example/a2a")
    assert store.watches == {}


def test_a_hashed_sk_key_provisions_and_BILLS_rather_than_suspending(
        tmp_path, monkeypatch):
    """The subtle one: a watch keyed on the raw secret failed to charge and
    suspended itself on the first cycle — the customer paid nothing and got
    nothing."""
    monkeypatch.setenv("GUILD_HASH_KEYS", "1")
    monkeypatch.setenv("GUILD_ALLOW_WEAK_KDF", "1")
    pricing.load_runtime({})
    store = Store(path=str(tmp_path / "hashed.json"))
    _rec, raw_key, acct_key = _funded_account(store)
    assert acct_key != raw_key, "this test must exercise the HASHED path"
    watch = indexops.provision_watch(store, raw_key, "https://a.example/a2a")
    assert watch["owner_key"] == acct_key
    assert raw_key not in str(store.watches), "a raw secret must never persist"

    def _charge(owner):
        store.charge_account(owner, 5, "watch_cycle")
        return 5

    out = indexops.run_watch_cycle(store, store.watches[watch["id"]],
                                   charge=_charge,
                                   runner=lambda url: _observed())
    assert out["suspended"] is False
    assert out["charged_credits"] == 5
    assert store.watches[watch["id"]]["active"] is True


def test_watch_ownership_is_keyed_on_the_account_not_the_secret(store):
    _rec, raw_key, acct_key = _funded_account(store)
    a = indexops.provision_watch(store, raw_key, "https://a.example/a2a")
    b = indexops.provision_watch(store, raw_key, "https://a.example/a2a/")
    assert a["id"] == b["id"] and b["created"] is False


# --------------------------------------------------------------------------
# 4. Stored XSS on the evidence page
# --------------------------------------------------------------------------
XSS = '"><script>alert(1)</script>'


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def test_hostile_registry_strings_cannot_inject_script(store, monkeypatch):
    """Every field on this page comes from a third-party registry or from the
    probed server's own response. The product IS publishing what other
    people's servers said."""
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "store", store)

    entry = trustindex.new_entry("https://evil.example/a2a", "mcp_registry",
                                 declared={"name": XSS, "capabilities": [XSS]})
    entry["sources"] = [{"source": XSS, "first_seen": "x", "last_seen": "y"}]
    trustindex.apply_observation(entry, {
        "verdict": XSS, "failed": [], "unknowns": [],
        "checks": [{"check": XSS, "status": "failed", "detail": XSS}]})
    entry["drift"] = [{"at": XSS, "from": XSS, "to": XSS}]
    store.trust_index[entry["id"]] = entry

    body = _client().get(f"/index/{entry['id']}/evidence").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body, "the payload must appear escaped, not dropped"


def test_endpoint_url_is_url_encoded_in_the_action_link(store, monkeypatch):
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "store", store)
    entry = trustindex.new_entry('https://evil.example/a2a', "s")
    entry["endpoint"] = 'https://evil.example/a2a?x="><script>alert(1)</script>'
    trustindex.apply_observation(entry, _observed())
    store.trust_index[entry["id"]] = entry
    body = _client().get(f"/index/{entry['id']}/evidence").text
    assert "<script>" not in body


def test_detail_json_url_encodes_action_urls(store, monkeypatch):
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "store", store)
    entry = trustindex.new_entry("https://a.example/a2a", "s")
    entry["endpoint"] = "https://a.example/a2a?q=a b&c=d"
    store.trust_index[entry["id"]] = entry
    out = _client().get(f"/index/{entry['id']}").json()
    assert " " not in out["actions"]["free_recheck_now"].split("url=")[1]


# --------------------------------------------------------------------------
# 5. A2A decision-boundary surface
# --------------------------------------------------------------------------
def _a2a(client, text: str):
    return client.post("/a2a", json={
        "jsonrpc": "2.0", "id": "1", "method": "message/send",
        "params": {"message": {"role": "user",
                               "parts": [{"kind": "text", "text": text}]}}})


def test_a2a_exposes_free_preflight(store, monkeypatch):
    import app.main as main_mod
    import app.a2a as a2a_mod
    monkeypatch.setattr(main_mod, "store", store)
    monkeypatch.setattr(a2a_mod, "store", store)
    monkeypatch.setattr(a2a_mod.preflight, "run",
                        lambda url, store=None: _observed())
    r = _a2a(_client(), "preflight: https://some-agent.example/a2a")
    assert r.status_code == 200
    body = r.json()["result"]
    assert "verdict" in str(body)


def test_a2a_exposes_the_index(store, monkeypatch):
    import app.main as main_mod
    import app.a2a as a2a_mod
    monkeypatch.setattr(main_mod, "store", store)
    monkeypatch.setattr(a2a_mod, "store", store)
    e = trustindex.new_entry("https://a.example/a2a", "mcp_registry")
    store.trust_index[e["id"]] = e
    r = _a2a(_client(), "index")
    assert r.status_code == 200
    assert "trust_index" in str(r.json()["result"])


def test_agent_card_advertises_the_decision_skills():
    body = _client().get("/.well-known/agent-card.json").json()
    ids = {s["id"] for s in body.get("skills", [])}
    assert {"guild.preflight", "guild.preflight.deep", "guild.index"} <= ids


def test_a_url_bearing_preflight_is_not_treated_as_an_advert(store, monkeypatch):
    """`preflight: https://…` carries a URL and would otherwise be read as
    someone advertising themselves."""
    import app.main as main_mod
    import app.a2a as a2a_mod
    monkeypatch.setattr(main_mod, "store", store)
    monkeypatch.setattr(a2a_mod, "store", store)
    monkeypatch.setattr(a2a_mod.preflight, "run",
                        lambda url, store=None: _observed())
    body = str(_a2a(_client(), "preflight: https://x.example/a2a").json())
    assert "endpoint_declare" not in body


# --------------------------------------------------------------------------
# 6. Product copy leads with the decision
# --------------------------------------------------------------------------
def test_landing_leads_with_allow_caution_block():
    body = _client().get("/", headers={"accept": "text/html"}).text
    assert "safely use or pay this endpoint" in body
    assert "/preflight" in body
    assert "Reputation is the product" not in body


def test_llms_txt_leads_with_the_decision_not_passport_issuance():
    body = _client().get("/llms.txt").text
    head = body[:900]
    assert "allow, caution or block" in head
    assert "/index" in head
