"""v0.2 tests — costly attestations: receipts, evidence weighting, collusion,
staking/slashing, and the evidence/flags endpoints.

Scoring properties are tested against the engine directly (deterministic, no
global state); the new HTTP surface is tested via FastAPI's TestClient.
"""
import os
os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.reputation import score, AttRecord, ScoringParams  # noqa: E402

client = TestClient(app)


def _register(name, caps, seed=False, metadata=None):
    r = client.post("/agents/register", json={
        "name": name, "capabilities": caps, "seed": seed, "metadata": metadata or {},
    })
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# Engine — scoring properties
# --------------------------------------------------------------------------- #
def test_backed_attestation_outweighs_unbacked():
    # Same reviewer, same 0.9 rating, but one is backed by a real receipt and one
    # is a bare assertion. The backed subject must score strictly higher.
    recs = [
        AttRecord("seed", "backed", 0.9, weight=0.85),
        AttRecord("seed", "unbacked", 0.9, weight=0.15),
    ]
    res = score(["seed", "backed", "unbacked"], recs, seed_ids=["seed"])
    assert res.scores["backed"].trust > res.scores["unbacked"].trust


def test_collusion_ring_is_flagged_and_penalised():
    # Two agents 5-star each other with rich evidence; no seed ever vouches.
    recs = [
        AttRecord("c1", "c2", 1.0, weight=1.0, stake=1.0),
        AttRecord("c2", "c1", 1.0, weight=1.0, stake=1.0),
    ]
    res = score(["seed", "c1", "c2"], recs, seed_ids=["seed"])
    assert res.flags["c1"].suspicion >= 0.4
    assert res.flags["c2"].suspicion >= 0.4
    # Manufactured mutual praise cannot lift them above the low prior.
    assert res.scores["c1"].trust <= 25
    assert res.scores["c2"].trust <= 25


def test_sybil_farm_target_is_flagged():
    # Six fresh, zero-trust accounts uniformly 5-star one target.
    recs = [AttRecord(f"s{i}", "target", 1.0, weight=0.7) for i in range(6)]
    res = score(["seed", "target", *[f"s{i}" for i in range(6)]], recs, seed_ids=["seed"])
    assert res.flags["target"].suspicion >= 0.4
    assert res.scores["target"].trust <= 25


def test_dodged_detection_still_denies_trust():
    # A directed-star farm that deliberately dodges the threshold detector:
    # only 3 boosters (below the broad-farm count) and jittered ratings (high
    # variance, so the uniform-farm signal misses). Detection may NOT fire — but
    # the target must STILL gain no trust, because none of its praise traces back
    # to a seed. This is the defence that does not depend on catching the farm.
    recs = [
        AttRecord("b0", "target", 0.95, weight=1.0),
        AttRecord("b1", "target", 0.70, weight=1.0),
        AttRecord("b2", "target", 0.55, weight=1.0),
    ]
    res = score(["seed", "target", "b0", "b1", "b2"], recs, seed_ids=["seed"])
    s = res.scores["target"]
    assert s.trusted_attestations == 0      # no seed-anchored reviewer
    assert s.trust <= 25                     # pinned at the low prior regardless


def test_staking_slash_costs_the_issuer_more_than_it_helps_the_subject():
    base = [
        AttRecord("seed", "w", 0.30, weight=0.85),     # truth from a trusted seed
        AttRecord("seed", "a", 0.70, weight=0.85),     # the attacker has standing
    ]
    lie = AttRecord("a", "w", 1.0, weight=0.15, stake=3.0)  # false staked 5-star
    ids = ["seed", "w", "a"]
    no_lie = score(ids, base, seed_ids=["seed"]).scores
    with_lie = score(ids, base + [lie], seed_ids=["seed"]).scores

    subject_gain = with_lie["w"].trust - no_lie["w"].trust
    issuer_loss = no_lie["a"].trust - with_lie["a"].trust
    assert with_lie["a"].slash_penalty > 0
    assert issuer_loss > max(0.0, subject_gain)


def test_honest_worker_beats_attackers_end_to_end():
    # A miniature of the attack experiment, in the engine.
    seeds = ["S1", "S2", "S3"]
    recs = []
    for s in seeds:
        recs.append(AttRecord(s, "honest", 0.92, weight=0.85))
        recs.append(AttRecord(s, "cheap", 0.34, weight=0.85))
    # colluders + sybil farm, all 5-star, no seed inflow
    recs += [AttRecord("c1", "c2", 1.0, weight=1.0), AttRecord("c2", "c1", 1.0, weight=1.0)]
    recs += [AttRecord(f"f{i}", "promoted", 1.0, weight=1.0) for i in range(5)]
    ids = seeds + ["honest", "cheap", "c1", "c2", "promoted", *[f"f{i}" for i in range(5)]]
    sc = score(ids, recs, seed_ids=seeds).scores
    workers = ["honest", "cheap", "c1", "c2", "promoted"]
    top = max(workers, key=lambda a: sc[a].trust)
    assert top == "honest"
    assert sc["honest"].trust > max(sc["c1"].trust, sc["c2"].trust, sc["promoted"].trust)


# --------------------------------------------------------------------------- #
# API — task receipts, evidence and flags endpoints
# --------------------------------------------------------------------------- #
def test_task_receipt_lifecycle_and_evidence_weight():
    requester = _register("Hirer", ["research"])
    worker = _register("Doer", ["fact-check"])

    # create a paid task
    r = client.post("/tasks", headers={"X-API-Key": requester["api_key"]}, json={
        "requester_id": requester["id"], "worker_id": worker["id"],
        "task_type": "fact-check", "payment": 0.03,
    })
    assert r.status_code == 200, r.text
    task = r.json()
    assert task["outcome"] == "open"

    # submit the deliverable receipt
    r = client.post(f"/tasks/{task['id']}/receipt",
                    headers={"X-API-Key": worker["api_key"]},
                    json={"deliverable_hash": "0xabc123", "outcome": "delivered"})
    assert r.status_code == 200, r.text
    assert r.json()["deliverable_hash"] == "0xabc123"

    # attest against the receipt
    r = client.post("/attestations", headers={"X-API-Key": requester["api_key"]},
                    json={"issuer_id": requester["id"], "subject_id": worker["id"],
                          "capability": "fact-check", "rating": 0.9,
                          "task_id": task["id"], "stake": 1.0})
    assert r.status_code == 200, r.text

    # evidence shows a backed, heavily-weighted attestation
    ev = client.get(f"/agents/{worker['id']}/evidence").json()
    assert ev["verified_task_count"] >= 1
    weights = [a["evidence_weight"] for a in ev["attestations"] if a["task_id"] == task["id"]]
    assert weights and weights[0] > 0.8   # receipt + payment + stake


def test_unbacked_attestation_is_low_weight():
    issuer = _register("Asserter", ["research"])
    subject = _register("Claimed", ["fact-check"])
    client.post("/attestations", headers={"X-API-Key": issuer["api_key"]},
                json={"issuer_id": issuer["id"], "subject_id": subject["id"],
                      "capability": "fact-check", "rating": 1.0})  # no task_id
    ev = client.get(f"/agents/{subject['id']}/evidence").json()
    assert all(a["evidence_weight"] < 0.3 for a in ev["attestations"])


def test_flags_endpoint_lists_collusion():
    a = _register("RingX", ["fact-check"])
    b = _register("RingY", ["fact-check"])
    for _ in range(3):
        client.post("/attestations", headers={"X-API-Key": a["api_key"]},
                    json={"issuer_id": a["id"], "subject_id": b["id"],
                          "capability": "fact-check", "rating": 1.0})
        client.post("/attestations", headers={"X-API-Key": b["api_key"]},
                    json={"issuer_id": b["id"], "subject_id": a["id"],
                          "capability": "fact-check", "rating": 1.0})
    flagged = client.get("/flags").json()
    names = {f["name"] for f in flagged["flagged"]}
    assert "RingX" in names and "RingY" in names
