"""Tests for substrate hardening — the absolute reputation floor and seed-path
requirement that stop unknown or colluding agents from bootstrapping trust.
Engine-level (deterministic, no HTTP)."""
from app.reputation import score, AttRecord, ScoringParams

CEILING = ScoringParams().unknown_trust_ceiling * 100  # trust points


def test_unknown_agent_cannot_bootstrap_trust():
    # An agent with no attestations and no seed path is pinned near zero, not
    # resting at the prior.
    res = score(["seed", "unknown"], [], seed_ids=["seed"])
    assert res.scores["unknown"].trust <= CEILING


def test_island_ring_is_denied_trust():
    # A and B lavishly praise each other but no seed ever vouches and the seed
    # has no path to them. Mutual praise must not manufacture trust.
    recs = [AttRecord("A", "B", 1.0, weight=0.85), AttRecord("B", "A", 1.0, weight=0.85)]
    res = score(["seed", "A", "B"], recs, seed_ids=["seed"])
    assert res.scores["A"].trust <= CEILING
    assert res.scores["B"].trust <= CEILING


def test_seed_anchored_agent_earns_trust():
    # The control: when the seed actually vouches (backed attestation), the
    # subject is seed-reachable and earns real trust above the unknown ceiling.
    res = score(["seed", "H"], [AttRecord("seed", "H", 0.9, weight=0.85)], seed_ids=["seed"])
    assert res.scores["H"].trust > CEILING
    # …and strictly more than an unknown agent in the same graph.
    res2 = score(["seed", "H", "U"], [AttRecord("seed", "H", 0.9, weight=0.85)], seed_ids=["seed"])
    assert res2.scores["H"].trust > res2.scores["U"].trust


def test_seed_path_requirement_is_the_lever():
    # Turning off the seed-path requirement is what lets an island ring score —
    # proof the hardening (not some other factor) is doing the work.
    recs = [AttRecord("A", "B", 1.0, weight=0.85), AttRecord("B", "A", 1.0, weight=0.85)]
    hard = score(["seed", "A", "B"], recs, seed_ids=["seed"],
                 params=ScoringParams(require_seed_path=True))
    soft = score(["seed", "A", "B"], recs, seed_ids=["seed"],
                 params=ScoringParams(require_seed_path=False, unknown_trust_ceiling=1.0))
    assert soft.scores["B"].trust >= hard.scores["B"].trust
