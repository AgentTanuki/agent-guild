"""Third-round corrections: the impression boundary, and DID coalescing.

Both defects were the same species — a claim in a docstring that the code did
not implement, where the gap flattered us:

  1. exposure to a PAID price was being counted from FREE product use, so the
     engine could halve or kill a price nobody had ever been shown;
  2. the module promised endpoint-then-identity deduplication and performed
     endpoint deduplication only, so one subject at several addresses inflated
     the one number the index is judged on.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import experiments, indexops, pricing, trustindex  # noqa: E402
from app.store import Store  # noqa: E402

EXT_UA = "a2a:langchain/0.2.1"


@pytest.fixture()
def store(tmp_path) -> Store:
    pricing.load_runtime({})
    return Store(path=str(tmp_path / "guild.json"))


def _challenge(store: Store, operation: str, actor: str, ua: str = EXT_UA):
    store.record_event(actor, "paid_offer_challenged", ua=ua,
                       endpoint="x402_challenge",
                       challenged_operation=operation, price_credits=20)


# --------------------------------------------------------------------------
# 1. Exposure to a PAID price means having been shown that price
# --------------------------------------------------------------------------
def test_free_preflight_is_not_exposure_to_the_deep_price(store):
    """THE defect: a free preflight caller has never been quoted the deep
    price, so counting them let the engine act on an offer nobody saw."""
    for i in range(40):
        store.record_event(f"a2a:net:free{i}", "preflight_run",
                           ua=f"a2a:langchain/0.2.{i}", endpoint="preflight")
    exp = experiments.qualified_exposure(store, "deep_preflight")
    assert exp["qualified_actors"] == 0, exp
    assert exp["paid_offers_shown"] == 0


def test_free_preflight_alone_cannot_make_deep_pricing_decisive(store):
    e = experiments.define(store, "deep", hypothesis="h",
                           variable="price:deep_preflight",
                           baseline={m: 0 for m in experiments.PRIMARY_METRICS})
    e["min_qualified"] = 3
    store.experiments["deep"] = e
    for i in range(50):
        store.record_event(f"a2a:net:free{i}", "preflight_run",
                           ua=f"a2a:langchain/0.2.{i}", endpoint="preflight")
    out = experiments.evaluate(store, "deep")
    assert out["decision"] in ("hold", "insufficient_evidence")
    assert out["decision"] != "kill"
    before = pricing.price("deep_preflight")
    experiments.apply_next_action(store)
    assert pricing.price("deep_preflight") == before, \
        "a price nobody was offered must not move"


def test_a_genuine_external_deep_challenge_IS_decisive(store):
    """The other half: real exposure to the real price must be able to decide."""
    e = experiments.define(store, "deep", hypothesis="h",
                           variable="price:deep_preflight",
                           baseline={m: 0 for m in experiments.PRIMARY_METRICS})
    e["min_qualified"] = 2
    store.experiments["deep"] = e
    _challenge(store, "deep_preflight", "a2a:net:shown1", "a2a:langchain/0.2.1")
    _challenge(store, "deep_preflight", "a2a:net:shown2", "a2a:crewai/1.0")
    exp = experiments.qualified_exposure(store, "deep_preflight")
    assert exp["qualified_actors"] == 2
    assert exp["paid_offers_shown"] == 2
    assert experiments.evaluate(store, "deep")["decision"] == "kill"


def test_a_challenge_for_another_operation_is_not_exposure(store):
    _challenge(store, "evidence_bundle", "a2a:net:other")
    assert experiments.qualified_exposure(
        store, "deep_preflight")["qualified_actors"] == 0
    assert experiments.qualified_exposure(
        store, "evidence_bundle")["qualified_actors"] == 1


def test_crawler_challenges_are_not_exposure(store):
    _challenge(store, "deep_preflight", "a2a:net:bot",
               ua="a2a:AgenstryBot/0.3.0")
    assert experiments.qualified_exposure(
        store, "deep_preflight")["qualified_actors"] == 0


def test_a_paid_completion_also_counts_as_exposure(store):
    """Someone who saw the price and PAID it has certainly been exposed."""
    store.record_event("a2a:net:payer", "deep_preflight_run", ua=EXT_UA,
                       endpoint="preflight_deep", settlement_mode="x402",
                       settlement_confirmed=True, settlement_mainnet=True)
    exp = experiments.qualified_exposure(store, "deep_preflight")
    assert exp["qualified_actors"] == 1
    assert exp["paid_completions"] == 1


def test_a_free_tier_call_of_the_same_shape_is_not_a_completion(store):
    store.record_event("a2a:net:freebie", "deep_preflight_run", ua=EXT_UA,
                       endpoint="preflight_deep", settlement_mode="free")
    assert experiments.qualified_exposure(
        store, "deep_preflight")["qualified_actors"] == 0


def test_evidence_bundle_and_watch_have_the_same_boundary(store):
    for op in ("evidence_bundle", "watch_cycle"):
        assert experiments.qualified_exposure(store, op)["qualified_actors"] == 0
    _challenge(store, "watch_cycle", "a2a:net:w1")
    assert experiments.qualified_exposure(
        store, "watch_cycle")["qualified_actors"] == 1
    assert experiments.qualified_exposure(
        store, "evidence_bundle")["qualified_actors"] == 0


def test_portfolio_view_is_labelled_as_not_valid_for_pricing(store):
    out = experiments.qualified_exposure(store)
    assert out["operation_scope"] == "all_surfaces"
    assert "NOT valid for pricing" in out["rule"]


# --------------------------------------------------------------------------
# 2. DID coalescing — one subject, several addresses
# --------------------------------------------------------------------------
DID_A = "did:key:z6MkExampleAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DID_B = "did:key:z6MkExampleBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


def test_two_endpoints_with_one_did_are_one_subject(store):
    out = indexops.ingest(store, [
        {"endpoint": "https://a.example/a2a", "source": "s1", "did": DID_A},
        {"endpoint": "https://b.example/a2a", "source": "s2", "did": DID_A},
    ])
    assert out["added"] == 1
    assert out["aliased_to_existing_did"] == 1
    assert len(store.trust_index) == 1
    entry = next(iter(store.trust_index.values()))
    assert [a["endpoint"] for a in entry["alias_endpoints"]] == \
        ["https://b.example/a2a"]


def test_alias_retains_its_own_provenance(store):
    indexops.ingest(store, [
        {"endpoint": "https://a.example/a2a", "source": "mcp_registry", "did": DID_A},
        {"endpoint": "https://b.example/a2a", "source": "guild_registration",
         "did": DID_A},
    ])
    entry = next(iter(store.trust_index.values()))
    alias = entry["alias_endpoints"][0]
    assert [s["source"] for s in alias["sources"]] == ["guild_registration"]
    # and the canonical entry records that the second source saw the subject
    assert {s["source"] for s in entry["sources"]} == {"mcp_registry",
                                                       "guild_registration"}


def test_different_dids_are_never_merged(store):
    indexops.ingest(store, [
        {"endpoint": "https://a.example/a2a", "source": "s", "did": DID_A},
        {"endpoint": "https://b.example/a2a", "source": "s", "did": DID_B},
    ])
    assert len(store.trust_index) == 2


def test_operator_equivalence_is_never_inferred_from_names(store):
    """Two endpoints of the SAME company, no declared identity. Guessing would
    launder one party's evidence into another's."""
    indexops.ingest(store, [
        {"endpoint": "https://api.acme.example/a2a", "source": "s",
         "declared": {"name": "Acme Agent"}},
        {"endpoint": "https://eu.acme.example/a2a", "source": "s",
         "declared": {"name": "Acme Agent"}},
    ])
    assert len(store.trust_index) == 2
    view = trustindex.public_view(next(iter(store.trust_index.values())))
    assert "never inferred" in view["identity"]["operator"]


def test_reconciliation_migrates_pre_existing_duplicates(store):
    """The index shipped keyed on endpoint alone, so duplicates already exist."""
    for host in ("a", "b", "c"):
        e = trustindex.new_entry(f"https://{host}.example/a2a", "s", did=DID_A)
        e["first_indexed_at"] = f"2026-07-{10 + ord(host) - 97}T00:00:00+00:00"
        store.trust_index[e["id"]] = e
    assert len(store.trust_index) == 3
    out = indexops.reconcile_identities(store)
    assert out["merged_into_canonical"] == 2
    assert len(store.trust_index) == 1
    canonical = next(iter(store.trust_index.values()))
    assert canonical["endpoint"] == "https://a.example/a2a", "oldest wins"
    assert len(canonical["alias_endpoints"]) == 2


def test_reconciliation_is_idempotent(store):
    for host in ("a", "b"):
        e = trustindex.new_entry(f"https://{host}.example/a2a", "s", did=DID_A)
        store.trust_index[e["id"]] = e
    indexops.reconcile_identities(store)
    again = indexops.reconcile_identities(store)
    assert again["merged_into_canonical"] == 0
    assert len(store.trust_index) == 1
    assert len(next(iter(store.trust_index.values()))["alias_endpoints"]) == 1


def test_reconciliation_is_a_noop_without_declared_identity(store):
    for host in ("a", "b"):
        e = trustindex.new_entry(f"https://{host}.example/a2a", "s")
        store.trust_index[e["id"]] = e
    assert indexops.reconcile_identities(store)["merged_into_canonical"] == 0
    assert len(store.trust_index) == 2


def test_summary_reports_subjects_and_endpoints_separately(store):
    indexops.ingest(store, [
        {"endpoint": "https://a.example/a2a", "source": "s", "did": DID_A},
        {"endpoint": "https://b.example/a2a", "source": "s", "did": DID_A},
    ])
    summary = trustindex.summarise(store.trust_index.values())
    assert summary["total_entries"] == 1
    assert summary["alias_endpoints_folded"] == 1
    assert summary["distinct_endpoints_known"] == 2
    assert "operator identity is NEVER inferred" in summary["dedupe"]
