"""The one-call first-contact entry point (`GET /check` + `guild_check` MCP tool).

Locks the conversion lever: a brand-new agent must be able to go from "never
heard of the Guild" to a confident delegation decision *and* a reason to
contribute back in a single call — and that call must carry the honest,
provenance-labelled proof.
"""
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from app.store import Store  # noqa: E402
from app.bootstrap_eval import seed_bootstrap_evaluation  # noqa: E402


def _seeded_store():
    s = Store(path="")
    seed_bootstrap_evaluation(s)
    return s


def test_check_payload_shape_and_proof():
    s = _seeded_store()
    r = s.check("fact-check")
    # the whole first-contact story in one object
    for k in ("capability", "best_agent", "verdict", "shortlist", "proof",
              "why_trust_this", "how_to_contribute"):
        assert k in r, k
    # a populated graph returns a real best agent + a hire/caution/avoid verdict
    assert r["best_agent"] is not None
    assert r["verdict"]["recommendation"] in ("hire", "caution", "avoid")
    assert r["best_agent"]["id"] == r["shortlist"][0]["id"]
    # the proof block carries the honest provenance label (never a bare number)
    assert r["proof"]["dataset"] in ("bootstrap", "production", "mixed", "empty")
    assert r["proof"]["lift"] is not None
    assert "bootstrap" in r["proof"]["disclaimer"].lower()
    # and points the agent at the contribution loop
    assert "attest" in r["how_to_contribute"].lower()


def test_check_unknown_capability_is_graceful():
    s = _seeded_store()
    r = s.check("no-such-capability")
    assert r["best_agent"] is None
    assert r["verdict"] is None
    assert r["shortlist"] == []
    # proof + guidance still present so the caller still learns what the Guild is
    assert r["proof"]["lift"] is not None
    assert r["how_to_contribute"]


def test_check_matches_search_and_risk_paths():
    """The one-call path must agree with the granular endpoints it composes."""
    s = _seeded_store()
    r = s.check("research")
    best = r["best_agent"]
    assert best == s.shortlist("research", limit=3)[0]
    assert r["verdict"] == s.risk_for(best["id"])


def test_check_no_supply_returns_nearest_and_be_first():
    """A miss must never be a dead end: it routes to the nearest supplied
    capability and pitches the caller to become the first supplier."""
    s = _seeded_store()
    r = s.check("web-research")  # seeds carry 'research', not 'web-research'
    assert r["status"] == "no_supply_yet"
    assert r["best_agent"] is None
    # fuzzy routing finds the real supply under a similar name
    near_caps = [n["capability"] for n in r["nearest_capabilities"]]
    assert "research" in near_caps
    hit = next(n for n in r["nearest_capabilities"] if n["capability"] == "research")
    assert hit["shortlist"], "nearest capability must include its live shortlist"
    # the be-first recruitment block is present and actionable on both transports
    bf = r["be_first"]
    assert "web-research" in bf["register"]["http"]
    assert "guild_register" in bf["register"]["mcp"]


def test_check_hit_has_supply_status_and_no_be_first():
    s = _seeded_store()
    r = s.check("fact-check")
    assert r["status"] == "supply"
    assert "be_first" not in r
    assert "nearest_capabilities" not in r


def test_check_cold_start_supplier_gets_guild_next():
    """A supplier that exists but has no evidence yet (confidence ≈ 0) is a
    cold-start deadlock: nobody vouches first, so it stays unproven forever. The
    payload must name the situation honestly and hand the caller the exact
    first-attestation call — the loop that yields the first real attestation."""
    s = _seeded_store()
    rec = s.register_agent("ColdStartCo", ["widget-forging"], {})
    r = s.check("widget-forging")
    assert r["status"] == "supply"
    assert r["best_agent"]["confidence"] < 0.2
    gn = r["guild_next"]
    for k in ("situation", "high_value_action", "one_call", "or_attest", "why_free"):
        assert k in gn, k
    # honest framing: unproven, not flawed
    assert "unproven" in gn["situation"].lower()
    # actionable: the exact worker + capability the caller would attest
    assert rec["id"] in gn["one_call"] and "widget-forging" in gn["one_call"]
    assert rec["id"] in gn["or_attest"]


def test_check_proven_supplier_has_no_guild_next():
    """The cold-start nudge must NOT fire for an evidence-backed supplier —
    otherwise it would nag on every lookup and stop meaning anything."""
    s = _seeded_store()
    r = s.check("fact-check")  # seed supply carries real attestations
    assert r["best_agent"]["confidence"] >= 0.2
    assert "guild_next" not in r


def test_check_records_capability_demand_and_summary():
    """Every /check is recorded as dated demand; the summary separates
    supplied from unsupplied lookups so /capabilities can be honest."""
    s = _seeded_store()
    s.check("fact-check")
    s.check("web-research")
    s.check("web-research")
    d = s.demand_summary()
    assert d["fact-check"]["supplied_lookups"] == 1
    assert d["web-research"]["lookups"] == 2
    assert d["web-research"]["supplied_lookups"] == 0
    assert d["web-research"]["last_lookup"] is not None


def test_capability_index_counts_suppliers():
    s = _seeded_store()
    idx = s.capability_index()
    assert idx.get("fact-check", 0) >= 1


def test_check_leads_with_explanation_object_not_scalar():
    """§15: the one-call payload must lead with an explanation OBJECT (estimate,
    confidence, staleness, top evidence), never a bare scalar. The deprecated
    scalars remain under `verdict` for v1 callers."""
    s = _seeded_store()
    r = s.check("fact-check")
    d = r["decision"]
    assert d is not None
    for k in ("agent_id", "estimate", "confidence", "staleness", "top_evidence",
              "interpretation"):
        assert k in d, k
    assert 0.0 <= d["estimate"] <= 1.0
    assert isinstance(d["top_evidence"], list) and d["top_evidence"]
    assert d["agent_id"] == r["best_agent"]["id"]
    # back-compat: the deprecated scalar path is still there
    assert r["verdict"]["recommendation"] in ("hire", "caution", "avoid")
    assert "risk" in r["verdict"]["deprecated"]


def test_staleness_is_computed_not_null():
    """§15 required field: staleness must be a real object with an age + label
    once an agent has dated evidence — not the old hardcoded None."""
    s = _seeded_store()
    best = s.shortlist("fact-check", limit=1)[0]
    st = s.risk_for(best["id"])["staleness"]
    assert st is not None
    assert st["label"] in ("fresh", "aging", "stale", "unknown")
    assert "most_recent_at" in st


def test_check_no_supply_has_no_decision():
    s = _seeded_store()
    r = s.check("no-such-capability")
    assert r["decision"] is None


# --- reachability honesty (2026-07-10) --------------------------------------
# Live telemetry: an external agent asked `check: fact-check` ~29 times over
# 3 days because the reply said "hire Veritas-Prime" — an agent with no
# declared endpoint. An un-actionable recommendation turns /check into a poll.
# These tests lock the fix: the payload must say whether the recommended
# supply can actually be contacted, and must offer a watch instead of a poll.

def test_check_unreachable_supply_is_named_and_offers_watch():
    s = _seeded_store()
    r = s.check("fact-check")  # bootstrap agents declare no endpoint
    assert r["best_agent"]["reachable"] is False
    assert r["best_agent"]["contact"] is None
    assert r["decision"]["reachable"] is False
    block = r["reachability"]
    assert block["status"] == "supply_unreachable"
    assert "/demand/watch" in block["watch_now"]["watch"]
    assert "fact-check" in block["watch_now"]["watch"]
    # the honest answer never pretends there is a route
    assert "no route" in block["honest_answer"].lower()


def test_check_surfaces_best_reachable_when_top_is_not():
    s = _seeded_store()
    a = s.register_agent(name="ReachableFactChecker",
                         capabilities=["fact-check"],
                         metadata={"endpoint": "https://example.com/a2a"})
    r = s.check("fact-check")
    # top-ranked is still an endpoint-less bootstrap agent, but a contactable
    # supplier exists — the payload must point at the actionable one
    block = r["reachability"]
    assert block["status"] == "top_ranked_unreachable"
    assert block["best_reachable"]["id"] == a["id"]
    assert block["best_reachable"]["contact"] == "https://example.com/a2a"


def test_check_reachable_top_has_no_reachability_block():
    s = Store(path="")
    a = s.register_agent(name="OnlyFactChecker",
                         capabilities=["fact-check"],
                         metadata={"endpoint": "https://example.com/a2a"})
    r = s.check("fact-check")
    assert "reachability" not in r
    assert r["best_agent"]["id"] == a["id"]
    assert r["best_agent"]["reachable"] is True


def test_capability_demand_event_carries_reachable_supply():
    s = _seeded_store()
    s.check("fact-check")
    ev = [e for e in s.events if e["type"] == "capability_demand"][-1]
    assert ev["reachable_supply"] is False
    s.register_agent(name="R", capabilities=["fact-check"],
                     metadata={"endpoint": "https://example.com/x"})
    s.check("fact-check")
    ev = [e for e in s.events if e["type"] == "capability_demand"][-1]
    assert ev["reachable_supply"] is True


def test_reachability_status_never_overstates_verification():
    """A declared endpoint is a claim, not a route: the Boolean stays for
    compat, but reachability_status must say declared_endpoint (never a
    verified state — no verifier exists yet) and last_verified_at must be
    null. Guards against quietly promoting declarations to verified."""
    s = _seeded_store()
    a = s.register_agent(name="DeclaredOnly", capabilities=["fact-check"],
                         metadata={"endpoint": "https://example.com/a2a"})
    r = s.check("fact-check")
    entries = r["shortlist"] + [r["decision"]]
    for e in entries:
        assert e["reachability_status"] in ("unknown", "declared_endpoint")
        assert e["last_verified_at"] is None
        if e.get("reachable"):
            assert e["reachability_status"] == "declared_endpoint"
        else:
            assert e["reachability_status"] == "unknown"
