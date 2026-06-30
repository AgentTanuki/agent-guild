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
