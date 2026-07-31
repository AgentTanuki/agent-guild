"""Trust index, paid layer and experiment engine — the invariants.

The mandate names five things that must be PROVEN, not asserted: attribution,
paid issuance, idempotency, fail-closed behaviour, and crawler exclusion. Each
has a section below.

Every test here is written as a constraint on what the product may CLAIM. That
is deliberate: this codebase has already shipped three metrics that read better
than reality, and all three passed their functional tests.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import deepcheck, experiments, indexops, pricing, trustindex  # noqa: E402
from app.store import Store  # noqa: E402


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(path=str(tmp_path / "guild.json"))


def _observed(verdict="no_failed_checks", failed=(), unknowns=(),
              handshake="proven", reachable="proven"):
    return {
        "verdict": verdict,
        "failed": list(failed),
        "unknowns": list(unknowns),
        "checks": [
            {"check": "endpoint_reachable", "status": reachable, "detail": ""},
            {"check": "protocol_handshake", "status": handshake, "detail": ""},
        ],
    }


# --------------------------------------------------------------------------
# 1. Deduplication and provenance — inventory can never be inflated
# --------------------------------------------------------------------------
def test_same_endpoint_from_three_registries_is_one_entry(store):
    """One operator publishing to three registries is ONE endpoint with three
    provenance records. Counting it three times would inflate the only number
    the index is judged on."""
    recs = [
        {"endpoint": "https://Example.com:443/a2a/", "source": "mcp_registry"},
        {"endpoint": "https://example.com/a2a", "source": "a2a_registry"},
        {"endpoint": "https://example.com/a2a?utm=x", "source": "guild_registration"},
    ]
    out = indexops.ingest(store, recs)
    assert out["added"] == 1, out
    assert out["provenance_updated"] == 2
    assert len(store.trust_index) == 1
    entry = next(iter(store.trust_index.values()))
    assert len(entry["sources"]) == 3


def test_ingest_is_idempotent(store):
    recs = [{"endpoint": "https://example.com/a2a", "source": "mcp_registry"}]
    indexops.ingest(store, recs)
    second = indexops.ingest(store, recs)
    assert second["added"] == 0
    assert len(store.trust_index) == 1


def test_inventory_is_never_described_as_adoption(store):
    indexops.ingest(store, [{"endpoint": "https://a.example/a2a",
                             "source": "mcp_registry"}])
    summary = trustindex.summarise(store.trust_index.values())
    assert "claim" in summary["claim_vs_observation"].lower()
    assert "never reported as adoption" in summary["claim_vs_observation"]


# --------------------------------------------------------------------------
# 2. A listing is never promoted to an observation
# --------------------------------------------------------------------------
def test_a_listing_is_reported_as_a_claim_not_a_status(store):
    indexops.ingest(store, [{"endpoint": "https://a.example/a2a",
                             "source": "mcp_registry",
                             "declared": {"name": "Claims To Work"}}])
    entry = next(iter(store.trust_index.values()))
    view = trustindex.public_view(entry)
    assert view["status"] == trustindex.STATUS_INDEXED
    assert view["observed"] is None
    assert "NEVER CALLED" in view["observed_note"]
    assert "NOT verified" in view["claimed"]["note"]


def test_http_200_without_handshake_is_degraded_not_live():
    """The 92.9%/33.9% gap, encoded. A server answering 200 is not a working
    agent, and the index must never record it as one."""
    status = trustindex.status_from_preflight(
        _observed(verdict="do_not_delegate", failed=["protocol_handshake"],
                  handshake="failed"))
    assert status == trustindex.STATUS_DEGRADED


def test_unreachable_is_its_own_status():
    status = trustindex.status_from_preflight(
        _observed(verdict="do_not_delegate", failed=["endpoint_reachable"],
                  reachable="failed", handshake="unknown"))
    assert status == trustindex.STATUS_UNREACHABLE


def test_drift_is_recorded_on_every_status_change(store):
    indexops.ingest(store, [{"endpoint": "https://a.example/a2a",
                             "source": "s1"}])
    fp = next(iter(store.trust_index))
    indexops.recheck_one(store, fp, runner=lambda url: _observed())
    indexops.recheck_one(store, fp, runner=lambda url: _observed(
        verdict="do_not_delegate", failed=["protocol_handshake"],
        handshake="failed"))
    entry = store.trust_index[fp]
    assert entry["drift"], "a state change must be recorded"
    assert entry["drift"][-1]["to"] == trustindex.STATUS_DEGRADED


def test_stale_observations_are_labelled_stale(store, monkeypatch):
    indexops.ingest(store, [{"endpoint": "https://a.example/a2a", "source": "s"}])
    fp = next(iter(store.trust_index))
    indexops.recheck_one(store, fp, runner=lambda url: _observed())
    assert trustindex.public_view(store.trust_index[fp])["stale"] is False
    monkeypatch.setenv("GUILD_INDEX_FRESH_TTL_S", "300")
    store.trust_index[fp]["observed_at"] = "2020-01-01T00:00:00+00:00"
    assert trustindex.public_view(store.trust_index[fp])["stale"] is True


# --------------------------------------------------------------------------
# 3. No SEO spam — pages only where there is real evidence
# --------------------------------------------------------------------------
def test_no_evidence_page_for_an_endpoint_we_never_called(store):
    indexops.ingest(store, [{"endpoint": "https://a.example/a2a", "source": "s"}])
    entry = next(iter(store.trust_index.values()))
    assert trustindex.is_page_worthy(entry) is False


def test_evidence_page_allowed_once_we_have_observed(store):
    indexops.ingest(store, [{"endpoint": "https://a.example/a2a", "source": "s"}])
    fp = next(iter(store.trust_index))
    indexops.recheck_one(store, fp, runner=lambda url: _observed())
    assert trustindex.is_page_worthy(store.trust_index[fp]) is True


# --------------------------------------------------------------------------
# 4. Paid issuance FAILS CLOSED
# --------------------------------------------------------------------------
def test_evidence_bundle_refuses_without_a_ledger_anchor(store, monkeypatch):
    """A bundle that cannot be anchored must not be issued at all. Selling a
    degraded evidence object is worse than selling nothing."""
    monkeypatch.setattr(store, "latest_checkpoint", lambda **kw: None)
    with pytest.raises(deepcheck.EvidenceIssuanceRefused):
        deepcheck.evidence_bundle(store, "https://a.example/a2a")


def test_evidence_bundle_refuses_when_anchoring_raises(store, monkeypatch):
    def _boom(**kw):
        raise RuntimeError("stale durable state")
    monkeypatch.setattr(store, "latest_checkpoint", _boom)
    with pytest.raises(deepcheck.EvidenceIssuanceRefused):
        deepcheck.evidence_bundle(store, "https://a.example/a2a")


def test_evidence_bundle_refuses_without_a_signing_identity(store, monkeypatch):
    monkeypatch.setattr(store, "guild_identity", lambda: {"did": "", "private_key": ""})
    with pytest.raises(deepcheck.EvidenceIssuanceRefused):
        deepcheck.evidence_bundle(store, "https://a.example/a2a")


def test_issued_bundle_verifies_offline_and_round_trips(store):
    bundle = deepcheck.evidence_bundle(store, "https://a.example/a2a")
    assert bundle["proof"]
    out = deepcheck.verify_bundle(store, bundle)
    assert out["signature_valid"] is True
    assert out["valid"] is True
    # tampering must break it
    tampered = json.loads(json.dumps(bundle))
    tampered["policy"]["decision"] = "allow"
    tampered["subject_endpoint"] = "https://attacker.example/a2a"
    assert deepcheck.verify_bundle(store, tampered)["signature_valid"] is False


def test_expired_bundle_is_invalid_but_still_signed(store):
    bundle = deepcheck.evidence_bundle(store, "https://a.example/a2a", ttl_s=60)
    bundle["valid_until"] = "2020-01-01T00:00:00+00:00"
    out = deepcheck.verify_bundle(store, bundle)
    assert out["expired"] is True
    assert out["valid"] is False


# --------------------------------------------------------------------------
# 5. Policy verdict never launders unknowns into a pass
# --------------------------------------------------------------------------
def test_blocking_failure_blocks():
    v = deepcheck.policy_verdict(
        {"failed": ["protocol_handshake"], "unknowns": []}, None)
    assert v["decision"] == "block"


def test_claim_failure_is_caution_not_block():
    v = deepcheck.policy_verdict(
        {"failed": ["agent_card_signed"], "unknowns": []}, None)
    assert v["decision"] == "caution"


def test_mostly_unknown_is_caution_not_allow():
    v = deepcheck.policy_verdict(
        {"failed": [], "unknowns": ["a", "b", "c", "d"]}, None)
    assert v["decision"] == "caution"
    assert "thin evidence" in v["reason"]


def test_recent_instability_downgrades_a_clean_result():
    entry = {"drift": [{"at": "x", "from": "live", "to": "degraded"},
                       {"at": "y", "from": "degraded", "to": "live"}]}
    v = deepcheck.policy_verdict({"failed": [], "unknowns": []}, entry)
    assert v["decision"] == "caution"
    assert "changed state" in v["reason"]


def test_policy_threshold_is_published_so_it_can_be_rejected():
    v = deepcheck.policy_verdict({"failed": [], "unknowns": []}, None)
    assert v["threshold"]
    assert "reject it" in v["caller_note"]


# --------------------------------------------------------------------------
# 6. Watch: idempotent provisioning, charge only for work done
# --------------------------------------------------------------------------
def test_provisioning_the_same_watch_twice_does_not_bill_twice(store):
    a = indexops.provision_watch(store, "key-1", "https://a.example/a2a")
    b = indexops.provision_watch(store, "key-1", "https://a.example/a2a/")
    assert a["id"] == b["id"]
    assert b["created"] is False
    assert len(store.watches) == 1


def test_different_callers_get_different_watches(store):
    a = indexops.provision_watch(store, "key-1", "https://a.example/a2a")
    b = indexops.provision_watch(store, "key-2", "https://a.example/a2a")
    assert a["id"] != b["id"]


def test_a_cycle_that_cannot_observe_is_not_billed(store):
    rec = indexops.provision_watch(store, "key-1", "https://a.example/a2a")
    charged = []
    store.watches[rec["id"]]["endpoint_id"] = "ep_does_not_exist"
    out = indexops.run_watch_cycle(
        store, store.watches[rec["id"]],
        charge=lambda k: charged.append(k) or 5)
    assert out["cycled"] is False
    assert charged == [], "a cycle that observed nothing must not be billed"


def test_failed_charge_suspends_the_watch_rather_than_serving_it_free(store):
    rec = indexops.provision_watch(store, "key-1", "https://a.example/a2a")

    def _broke(_key):
        raise RuntimeError("insufficient credits")

    out = indexops.run_watch_cycle(store, store.watches[rec["id"]],
                                   charge=_broke,
                                   runner=lambda url: _observed())
    assert out["suspended"] is True
    assert store.watches[rec["id"]]["active"] is False


def test_watch_records_a_change_only_when_status_actually_changes(store):
    rec = indexops.provision_watch(store, "key-1", "https://a.example/a2a")
    live = store.watches[rec["id"]]
    indexops.run_watch_cycle(store, live, runner=lambda url: _observed())
    first = len(store.watches[rec["id"]]["changes"])
    indexops.run_watch_cycle(store, store.watches[rec["id"]],
                             runner=lambda url: _observed())
    assert len(store.watches[rec["id"]]["changes"]) == first
    indexops.run_watch_cycle(
        store, store.watches[rec["id"]],
        runner=lambda url: _observed(verdict="do_not_delegate",
                                     failed=["protocol_handshake"],
                                     handshake="failed"))
    assert len(store.watches[rec["id"]]["changes"]) == first + 1


# --------------------------------------------------------------------------
# 7. Pricing is configuration, bounded
# --------------------------------------------------------------------------
def test_price_is_env_overridable(monkeypatch):
    monkeypatch.setenv("GUILD_PRICE_DEEP_PREFLIGHT", "45")
    assert pricing.price("deep_preflight") == 45


def test_price_override_is_clamped_to_its_ceiling(monkeypatch):
    monkeypatch.setenv("GUILD_PRICE_DEEP_PREFLIGHT", "999999")
    assert pricing.price("deep_preflight") == pricing.CEILINGS["deep_preflight"]


def test_malformed_price_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("GUILD_PRICE_DEEP_PREFLIGHT", "not-a-number")
    assert pricing.price("deep_preflight") == pricing.DEFAULTS["deep_preflight"]


def test_every_price_publishes_its_basis():
    table = pricing.table()
    for op, row in table["prices"].items():
        assert row["basis"], f"{op} has no stated basis"
        assert row["ceiling_credits"] >= row["credits"]


# --------------------------------------------------------------------------
# 8. ATTRIBUTION + CRAWLER EXCLUSION — an experiment cannot count our traffic
# --------------------------------------------------------------------------
def test_crawler_traffic_is_not_qualified_exposure(store):
    for _ in range(50):
        store.record_event("a2a:net:bot", "preflight_run",
                           ua="a2a:AgenstryBot/0.3.0", endpoint="preflight")
    assert experiments.qualified_exposure(store)["qualified_actors"] == 0


def test_first_party_traffic_is_not_qualified_exposure(store):
    store.record_event("ag-internal", "preflight_run",
                       ua="guild-release-gate", endpoint="preflight",
                       first_party=True)
    assert experiments.qualified_exposure(store)["qualified_actors"] == 0


def test_zero_qualified_exposure_never_produces_a_kill(store):
    """The category error this engine exists to prevent: '0% conversion on
    1,790 crawler impressions' is not a finding."""
    experiments.define(store, "exp-1", hypothesis="h", variable="price:deep_preflight",
                       baseline={"paid_decisions": 0})
    for _ in range(500):
        store.record_event("a2a:net:bot", "preflight_run",
                           ua="a2a:CrawlerBot/1.0", endpoint="preflight")
    out = experiments.evaluate(store, "exp-1")
    assert out["decision"] in ("hold", "insufficient_evidence")
    assert out["decision"] != "kill"


def test_supporting_metrics_can_never_promote(store):
    experiments.define(store, "exp-2", hypothesis="h", variable="price:deep_preflight",
                       baseline={m: 0 for m in experiments.PRIMARY_METRICS})
    # a mountain of free usage and inventory
    for i in range(200):
        e = trustindex.new_entry(f"https://x{i}.example/a2a", "mcp_registry")
        store.trust_index[e["id"]] = e
    out = experiments.evaluate(store, "exp-2")
    assert out["decision"] != "promote"


def test_revenue_definition_excludes_sandbox_credits(store):
    m = experiments.commercial_metrics(store)
    assert m["external_settled_revenue_usd"] == 0.0
    assert "Sandbox credits" in m["revenue_definition"]
    assert "not money" in m["revenue_definition"]


def test_kill_verdict_changes_the_offer_rather_than_celebrating_reach(store):
    exp = experiments.define(
        store, "exp-3", hypothesis="h", variable="price:deep_preflight",
        baseline={m: 0 for m in experiments.PRIMARY_METRICS})
    exp["min_qualified"] = 1
    store.experiments["exp-3"] = exp
    store.record_event("a2a:net:real", "deep_preflight_run",
                       ua="a2a:SomeRealAgent/1.0", endpoint="preflight_deep")
    action = experiments.next_action(store, "exp-3")
    if action["decision"] == "kill":
        assert action["action"] == "reprice"
        assert action["change"]["to_credits"] < action["change"]["from_credits"]
        assert action["change"]["within_ceiling"] is True


def test_insufficient_evidence_fixes_distribution_not_price(store):
    exp = experiments.define(
        store, "exp-4", hypothesis="h", variable="price:deep_preflight",
        baseline={m: 0 for m in experiments.PRIMARY_METRICS})
    exp["started_at"] = "2020-01-01T00:00:00+00:00"
    store.experiments["exp-4"] = exp
    action = experiments.next_action(store, "exp-4")
    assert action["decision"] == "insufficient_evidence"
    assert action["action"] == "increase_qualified_exposure"
    assert "never actually tested" in action["rationale"]


# --------------------------------------------------------------------------
# 9. Ownership is deterministic, never inferred
# --------------------------------------------------------------------------
def test_first_party_flag_is_respected_and_never_counted_external(store):
    indexops.ingest(store, [{"endpoint": "https://ours.example/a2a",
                             "source": "guild_registration",
                             "first_party": True}])
    entry = next(iter(store.trust_index.values()))
    assert entry["owner_class"] == trustindex.OWNER_FIRST_PARTY
    fp = entry["id"]
    indexops.recheck_one(store, fp, runner=lambda url: _observed())
    assert store.trust_index[fp]["owner_class"] == trustindex.OWNER_FIRST_PARTY


def test_unknown_ownership_is_never_promoted_to_external_without_observation(store):
    indexops.ingest(store, [{"endpoint": "https://who.example/a2a",
                             "source": "mcp_registry"}])
    entry = next(iter(store.trust_index.values()))
    assert entry["owner_class"] == trustindex.OWNER_UNKNOWN


# --------------------------------------------------------------------------
# 10. Bounded outbound behaviour
# --------------------------------------------------------------------------
def test_recheck_is_capped_per_cycle(store):
    for i in range(40):
        e = trustindex.new_entry(f"https://x{i}.example/a2a", "mcp_registry")
        store.trust_index[e["id"]] = e
    calls = []
    out = indexops.recheck_due(store, limit=5,
                               runner=lambda url: calls.append(url) or _observed())
    assert out["checked"] == 5
    assert len(calls) == 5


def test_remote_ingest_is_off_by_default(monkeypatch):
    from app import indexsources
    monkeypatch.delenv("GUILD_INDEX_INGEST", raising=False)
    assert indexsources.enabled() is False


def test_source_adapter_identifies_itself_truthfully():
    from app import indexsources
    assert "agent-guild" in indexsources.USER_AGENT
    assert "http" in indexsources.USER_AGENT  # contactable
    assert "Mozilla" not in indexsources.USER_AGENT  # never impersonates
