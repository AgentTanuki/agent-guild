"""The bootstrap evaluation proof-point.

These lock the honesty contract of `seed_bootstrap_evaluation` + the
provenance-aware `store.evaluation()`:

  * the seeded cohort makes `/evaluation` return a non-null, POSITIVE lift;
  * it is labelled `bootstrap` and kept OUT of the `production` lift;
  * seeding is idempotent;
  * outcomes track ground-truth quality, not the trust score (non-circular);
  * a genuine third-party graded task flips the dataset to `mixed` and lands in
    the `production` block, not `bootstrap`.
"""
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from app.store import Store  # noqa: E402
from app.bootstrap_eval import (  # noqa: E402
    seed_bootstrap_evaluation, already_seeded, BOOTSTRAP_TAG, ROSTER,
)


def _fresh():
    s = Store(path="")
    seed_bootstrap_evaluation(s)
    return s


def test_seeds_nonnull_positive_lift_labelled_bootstrap():
    s = _fresh()
    ev = s.evaluation()
    assert ev["dataset"] == "bootstrap"
    # the proof-point is no longer empty...
    assert ev["lift"] is not None
    # ...and recommended (high-trust) hires genuinely beat baseline
    assert ev["lift"] > 0
    assert ev["n_recommended"] > 0 and ev["n_baseline"] > 0
    # the bootstrap block carries the figure; production is still empty
    assert ev["bootstrap"]["lift"] is not None and ev["bootstrap"]["lift"] > 0
    assert ev["production"]["n_recommended"] == 0
    assert ev["production"]["n_baseline"] == 0
    assert ev["production"]["lift"] is None
    assert "bootstrap" in ev["disclaimer"].lower()


def test_idempotent():
    s = Store(path="")
    first = seed_bootstrap_evaluation(s)
    assert first["status"] == "seeded"
    n_tasks = len(s.tasks)
    second = seed_bootstrap_evaluation(s)
    assert second["status"] == "skipped"
    assert len(s.tasks) == n_tasks  # no duplicate cohort
    assert already_seeded(s)


def test_outcomes_track_quality_not_trust():
    """Non-circularity: accept-rate must follow each worker's ground-truth
    quality, which is set independently of (and before) any trust computation.
    The best-quality worker must out-accept the worst-quality worker."""
    s = _fresh()
    # map worker_id -> (true_quality, accepted, graded)
    by_worker: dict[str, list] = {}
    for a in s.agents.values():
        q = (a.get("metadata") or {}).get("true_quality")
        if q is not None:
            by_worker[a["id"]] = [q, 0, 0]
    for t in s.tasks.values():
        w = t["worker_agent_id"]
        if w in by_worker and t.get("outcome") in ("accepted", "disputed", "rejected"):
            by_worker[w][2] += 1
            if t["outcome"] == "accepted":
                by_worker[w][1] += 1
    rates = {wid: (acc / tot) for wid, (q, acc, tot) in by_worker.items() if tot}
    best = max(by_worker, key=lambda w: by_worker[w][0])
    worst = min(by_worker, key=lambda w: by_worker[w][0])
    assert rates[best] > rates[worst]


def test_production_task_flips_dataset_and_is_partitioned():
    s = _fresh()
    # two GENUINE outside agents (no first-party / seed / bootstrap tagging)
    emp = s.register_agent("Outside-Hirer", ["hiring"], metadata={})
    wrk = s.register_agent("Outside-Worker", ["research"], metadata={})
    task = s.create_task(emp["id"], wrk["id"], "research", payment=0.01, metadata={})
    s.submit_receipt(task["id"], "0xdeadbeef", outcome="accepted")

    ev = s.evaluation()
    assert ev["dataset"] == "mixed"
    # the production block now has exactly the one real graded task
    prod_n = ev["production"]["n_recommended"] + ev["production"]["n_baseline"]
    assert prod_n == 1
    # ...and it did NOT leak into the bootstrap block
    boot_n = ev["bootstrap"]["n_recommended"] + ev["bootstrap"]["n_baseline"]
    assert boot_n == len([t for t in s.tasks.values()
                          if (t.get("metadata") or {}).get(BOOTSTRAP_TAG)
                          and t.get("outcome") in ("accepted", "disputed", "rejected")])


def test_bootstrap_cohort_is_first_party_only():
    """Every seeded agent must be first-party, so none inflate organic metrics."""
    s = _fresh()
    seeded = [a for a in s.agents.values()
              if (a.get("metadata") or {}).get(BOOTSTRAP_TAG)]
    assert len(seeded) == sum(len(r) for r in ROSTER.values()) + 3  # workers + employers
    assert all(a.get("first_party") for a in seeded)
