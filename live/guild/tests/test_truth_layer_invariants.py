"""Truth-layer invariants — corrective pass 2026-07-31.

Three metric defects reached production and each of them made the numbers read
BETTER than reality:

  1. A passport LOOKUP MISS counted as an issuance, and one successful MCP call
     counted twice — so a schema probe took "genuine external passports issued"
     from 1 to 3 with no agent behind it.
  2. Aggregate stage activity was reported as a conversion funnel, so
     "0 followed / 1,790 served" read as a 0% conversion rate when 1,787 of
     those serves were unattributable crawlers and the qualified denominator
     was 1.
  3. ``/self-eval`` printed "FLYWHEEL TURNING — external agents pay" and a
     dollar figure derived from sandbox credits, while verified external
     revenue was $0.00.

These tests lock the corrections. They are deliberately written as assertions
about what the numbers MAY NOT claim, not about their current values.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import Store  # noqa: E402


def _tool_fn(tool):
    """Resolve the plain callable behind an MCP tool across fastmcp versions."""
    for attr in ("fn", "func", "__wrapped__"):
        f = getattr(tool, attr, None)
        if callable(f):
            return f
    return tool


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(path=str(tmp_path / "guild.json"))


def _register(store: Store, name: str, first_party: bool = False) -> tuple[str, str]:
    rec = store.register_agent(name, ["translation"], {})
    agent_id = rec.get("id") or rec.get("agent_id")
    if first_party:
        store.agents[agent_id]["first_party"] = True
    return agent_id, rec.get("api_key", "")


# --------------------------------------------------------------------------
# 1. Passport telemetry semantics
# --------------------------------------------------------------------------
def test_failed_passport_lookup_is_never_counted_as_an_issuance(store):
    """The exact 2026-07-30 probe behaviour: ask for a passport that cannot be
    produced. That is DEMAND, and must never appear as issuance."""
    before = sum(1 for e in store.events if e["type"] == "passport_issued")
    cred = store.issue_passport("agent_does_not_exist", actor_key="mcp",
                                surface="mcp", ua="mcp:schema-probe/0.1")
    after = sum(1 for e in store.events if e["type"] == "passport_issued")
    assert cred is None
    assert after == before, "a lookup miss was recorded as a passport issuance"


def test_successful_issuance_emits_exactly_one_event(store):
    agent_id, _ = _register(store, "subject-a")
    store.issue_passport(agent_id, actor_key="mcp", surface="mcp",
                         ua="mcp:client/1", request_id="req-1")
    issued = [e for e in store.events if e["type"] == "passport_issued"]
    assert len(issued) == 1, f"expected exactly one event, got {len(issued)}"
    ev = issued[0]
    assert ev["subject_id"] == agent_id
    assert ev["transport"] == "mcp"
    assert ev["request_id"] == "req-1"
    assert "self_claim" in ev


def test_mcp_tool_does_not_double_count(store, monkeypatch):
    """Regression for the double-record: the MCP path recorded on ENTRY and
    Store.issue_passport recorded again on success."""
    import app.state as state
    import app.mcp_server as mcp_server

    monkeypatch.setattr(state, "store", store)
    monkeypatch.setattr(mcp_server, "store", store)
    agent_id, _ = _register(store, "subject-b")

    fn = _tool_fn(mcp_server.guild_passport)
    fn(agent_id=agent_id, ctx=None)

    issued = [e for e in store.events if e["type"] == "passport_issued"]
    requested = [e for e in store.events if e["type"] == "passport_requested"]
    assert len(issued) == 1, "the MCP path double-counted the issuance"
    assert len(requested) == 1, "the attempt must be recorded separately"


def test_mcp_miss_records_a_failure_not_an_issuance(store, monkeypatch):
    import app.state as state
    import app.mcp_server as mcp_server

    monkeypatch.setattr(state, "store", store)
    monkeypatch.setattr(mcp_server, "store", store)

    out = _tool_fn(mcp_server.guild_passport)(agent_id="nope", ctx=None)
    assert "error" in out
    assert not [e for e in store.events if e["type"] == "passport_issued"]
    assert [e for e in store.events if e["type"] == "passport_issue_failed"]


def test_passport_activity_never_reports_one_adoption_number(store):
    agent_id, _ = _register(store, "subject-c")
    store.issue_passport(agent_id, actor_key="third-party-key", surface="http")
    act = store.passport_activity()
    assert set(act["behaviours"]) == {
        "subject_self_claim", "third_party_fetch",
        "third_party_verification", "subject_evidence_attached"}
    # a third party fetching someone else's credential is NOT a self claim
    assert act["behaviours"]["subject_self_claim"]["external"] == 0
    blob = repr(act).lower()
    assert "adoption" not in blob or "not" in blob


def test_third_party_fetch_is_not_a_self_claim(store):
    """A probe pulling another agent's public credential must never be counted
    as that agent adopting one."""
    subject, _ = _register(store, "subject-d")
    store.issue_passport(subject, actor_key="a2a:net:deadbeef", surface="a2a")
    ev = [e for e in store.events if e["type"] == "passport_issued"][-1]
    assert ev["self_claim"] is False


# --------------------------------------------------------------------------
# 2. Qualified cohort funnel
# --------------------------------------------------------------------------
def test_qualified_funnel_excludes_crawlers_and_first_party(store):
    store.record_event("a2a:net:crawler", "offer_served", ua="a2a:AgenstryBot/0.3.0",
                       offer="passport", endpoint="agent_card")
    store.record_event("ag-internal", "offer_served", ua="guild-release-gate",
                       offer="passport", endpoint="agent_card")
    q = store.qualified_passport_funnel()
    assert q["cohort"]["qualified_distinct_actors"] == 0
    assert "crawler" in q["excluded"].lower()


def test_qualified_funnel_deduplicates_repeat_exposure(store):
    """800 hits from one bot is ONE exposure, not 800 trials."""
    for _ in range(50):
        store.record_event("a2a:net:abc123", "offer_served",
                           ua="a2a:SomeAgent/1.0", offer="passport",
                           endpoint="agent_card")
    q = store.qualified_passport_funnel()
    assert q["cohort"]["raw_qualified_serves"] >= q["cohort"][
        "qualified_deduplicated_exposures"]


def test_zero_denominator_reports_not_measurable_never_zero_percent(store):
    """The headline correction: with no qualified exposure we must say the rate
    is NOT MEASURABLE, never '0% conversion'."""
    q = store.qualified_passport_funnel()
    nb = q["next_boundary"]
    assert nb["measurable"] is False
    assert "not measurable" in nb["reason"].lower()
    assert "rate" not in nb


def test_anonymous_exposure_is_unlinkable_not_a_failed_conversion(store):
    for _ in range(5):
        store.record_event(None, "offer_served", ua="a2a:Anon/1.0",
                           offer="passport", endpoint="llms_txt")
    q = store.qualified_passport_funnel()
    assert q["cohort"]["anonymous_unlinkable_serves"] >= 0
    assert "unlinkable" in q["honesty"].lower()


def test_small_sample_is_labelled_an_anecdote(store):
    """A boundary measured on a single-digit denominator must say so."""
    store.record_event("a2a:net:solo", "offer_served", ua="a2a:Solo/1.0",
                       offer="passport", endpoint="agent_card")
    store.record_event("a2a:net:solo", "register", ua="a2a:Solo/1.0")
    q = store.qualified_passport_funnel()
    nb = q["next_boundary"]
    if nb.get("measurable") and nb.get("n", 0) < 10:
        assert "ANECDOTE" in nb.get("sample_adequacy", "")


def test_raw_stages_are_labelled_as_not_a_conversion_funnel(store):
    f = store.passport_funnel()
    assert "qualified" in f
    assert "not a conversion funnel" in f["reading_guide"].lower()


# --------------------------------------------------------------------------
# 3. Self-evaluation honesty
# --------------------------------------------------------------------------
def test_health_never_reports_sandbox_credits_as_usd_revenue(store):
    v = store._health_vector()
    assert "revenue_usd_external" not in v, (
        "the sandbox-credit dollar line must not exist under a revenue name")
    assert v["verified_external_revenue_usd"] == 0.0
    assert "sandbox_credits_spent_external_NOT_MONEY" in v


def test_health_utility_uses_the_production_block(store):
    v = store._health_vector()
    assert "production_measured_lift" in v
    assert "production_n_recommended" in v
    # the mixed/seeded number may exist, but only under a name that cannot be
    # quoted as a production result
    assert "measured_lift" not in v or v.get("mixed_bootstrap_lift_NOT_PRODUCTION") is not None


def test_flywheel_verdict_requires_adoption_AND_verified_revenue(store):
    base = {"agents_external": 5, "external_querying_agents": 3,
            "external_repeat_query_agents": 2, "external_paid_queries": 99,
            "adoption_grade_external_self_claims": 0,
            "external_settled_revenue_usd": 0.0}

    # sandbox paid reads alone must NOT produce a flywheel verdict
    assert "FLYWHEEL" not in Store._verdict(dict(base), {})

    # adoption without money
    v = dict(base, adoption_grade_external_self_claims=2)
    assert "FLYWHEEL" not in Store._verdict(v, {})
    assert "ADOPTION WITHOUT REVENUE" in Store._verdict(v, {})

    # money without adoption - external settled revenue (confirmed mainnet,
    # payer not positively first-party) is the revenue half since the
    # 2026-08-31 revenue-semantics correction; attestation is not required
    v = dict(base, external_settled_revenue_usd=25.0)
    assert "FLYWHEEL" not in Store._verdict(v, {})
    assert "REVENUE WITHOUT ADOPTION" in Store._verdict(v, {})

    # both, and moving
    v = dict(base, adoption_grade_external_self_claims=2,
             external_settled_revenue_usd=25.0)
    out = Store._verdict(v, {"external_settled_revenue_usd": 25.0})
    assert "FLYWHEEL TURNING" in out


def test_verdict_names_the_missing_half_when_nothing_is_proven(store):
    v = {"agents_external": 5, "external_querying_agents": 3,
         "external_repeat_query_agents": 2, "external_paid_queries": 99,
         "adoption_grade_external_self_claims": 0,
         "verified_external_revenue_usd": 0.0}
    out = Store._verdict(v, {})
    assert "NOT evidence" in out or "not evidence" in out.lower()
    assert "$0.00" in out
