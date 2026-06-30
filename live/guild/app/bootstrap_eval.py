"""Bootstrap evaluation cohort — make `GET /evaluation` a defensible, NON-NULL,
clearly-labelled demonstration that hiring high-trust agents beats baseline,
*before* any live third-party traffic exists.

Why this exists
---------------
`/evaluation` is the Guild's strongest adoption argument: "don't trust us,
measure us." A sceptical agent doing due diligence calls it to verify the Guild
actually improves outcomes. Until now it returned ``lift: null`` because the seed
graph recorded task *receipts* but never *graded outcomes* (accepted / disputed /
rejected) — so there was nothing to measure. This module fills that gap honestly.

The honesty contract (this is the whole point)
----------------------------------------------
* **Labelled, never disguised.** Every agent and task created here is tagged
  ``first_party`` + ``seed_supply`` + ``bootstrap_eval``. ``store.evaluation()``
  partitions these OUT of the ``production`` lift and labels the dataset
  ``bootstrap``, so the seeded demonstration can never be mistaken for evidence
  from real outside agents.
* **Non-circular.** Task outcomes are sampled from each worker's *ground-truth
  quality*, independently of the Guild's own trust score. Trust is earned from
  honest, receipt-backed quality attestations; outcomes come from the same
  ground truth. The measured lift therefore *emerges* only because higher-quality
  workers genuinely succeed more often — it is not hand-set.
* **Reproducible.** Fixed RNG seed → identical cohort every run, so anyone can
  re-derive the number.
* **Idempotent.** If a bootstrap cohort already exists, this is a no-op.

It demonstrates the mechanism. It is not, and is never presented as, evidence
from real third-party traffic. See the ``dataset`` / ``disclaimer`` fields that
``store.evaluation()`` returns.
"""
from __future__ import annotations

import hashlib
import random
from typing import Any

# Tag stamped on every bootstrap agent and task. store.evaluation() and the
# adoption funnel both key off this (plus seed_supply / first_party) to keep the
# cohort out of organic / production metrics.
BOOTSTRAP_TAG = "bootstrap_eval"

# capability -> [(name, ground_truth_quality)] — a realistic spread from
# excellent to near-useless. Quality drives BOTH the honest attestation rating
# (so trust is earned) AND the sampled task outcome (so the lift is real). The
# two are linked only through ground truth, never to each other.
ROSTER: dict[str, list[tuple[str, float]]] = {
    "fact-check": [
        ("Veritas-Prime", 0.93), ("CiteCheck", 0.85),
        ("QuickFact", 0.60), ("RumorMill", 0.32),
    ],
    "code-review": [
        ("LintLord", 0.91), ("PRPilot", 0.80),
        ("NitPicker", 0.55), ("RubberStamp", 0.30),
    ],
    "research": [
        ("DeepDive", 0.90), ("ScholarBot", 0.77),
        ("SkimReader", 0.52), ("CopyPasta", 0.33),
    ],
    "summarization": [
        ("Distil", 0.88), ("TLDRpro", 0.72), ("Truncate", 0.48),
    ],
}


def _hash(*parts: Any) -> str:
    return "0x" + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16]


def already_seeded(store: Any) -> bool:
    """True if a bootstrap cohort is already present (so seeding is a no-op)."""
    return any((t.get("metadata") or {}).get(BOOTSTRAP_TAG) for t in store.tasks.values())


def seed_bootstrap_evaluation(
    store: Any,
    *,
    employers: int = 3,
    jobs_per_worker: int = 4,
    seed: int = 11,
) -> dict[str, Any]:
    """Seed a labelled, reproducible cohort of graded task outcomes so
    ``store.evaluation()`` returns a defensible, non-null *bootstrap* lift.

    Returns a small summary dict. No-op (``status: "skipped"``) if a bootstrap
    cohort already exists.
    """
    if already_seeded(store):
        return {"status": "skipped", "reason": "bootstrap cohort already present"}

    rng = random.Random(seed)

    # Pre-trusted employers anchor EigenTrust: trust can only propagate to a
    # worker along a path from a seed. Registering them seed=True also marks
    # them first-party, so they never count as organic demand.
    employer_recs = []
    for i in range(employers):
        employer_recs.append(store.register_agent(
            name=f"Bootstrap-Employer-{i + 1}",
            capabilities=["hiring"],
            metadata={"seed_supply": True, BOOTSTRAP_TAG: True, "role": "employer"},
            seed=True,
        ))

    workers = 0
    graded = 0
    for cap, roster in ROSTER.items():
        for name, quality in roster:
            worker = store.register_agent(
                name=name,
                capabilities=[cap],
                metadata={"seed_supply": True, BOOTSTRAP_TAG: True,
                          "true_quality": quality},
                first_party=True,
            )
            workers += 1
            for emp in employer_recs:
                for j in range(jobs_per_worker):
                    task = store.create_task(
                        emp["id"], worker["id"], cap, payment=0.01,
                        metadata={"seed_supply": True, BOOTSTRAP_TAG: True},
                    )
                    # Ground-truth outcome: success ~ Bernoulli(true_quality),
                    # sampled BEFORE and INDEPENDENTLY of any trust computation.
                    success = rng.random() < quality
                    if success:
                        outcome = "accepted"
                    else:
                        outcome = "disputed" if rng.random() < 0.5 else "rejected"
                    store.submit_receipt(
                        task["id"], _hash(worker["id"], emp["id"], j),
                        deliverable_url=None, outcome=outcome,
                    )
                    graded += 1
                    # Honest, receipt-backed quality attestation: rating tracks
                    # ground truth (with small observation noise), NOT the outcome
                    # and NOT the trust score. This is what lets trust be *earned*.
                    rating = max(0.0, min(1.0, quality + rng.uniform(-0.05, 0.05)))
                    store.add_custodial_attestation(
                        emp, worker, cap, rating, task["id"],
                        comment="bootstrap-eval", stake=0.0,
                    )

    ev = store.evaluation()
    return {
        "status": "seeded",
        "employers": len(employer_recs),
        "workers": workers,
        "graded_tasks": graded,
        "dataset": ev.get("dataset"),
        "lift": ev.get("lift"),
        "recommended_success_rate": ev.get("recommended_success_rate"),
        "baseline_success_rate": ev.get("baseline_success_rate"),
    }


if __name__ == "__main__":  # pragma: no cover - manual local seeding
    import json
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from app.state import store  # type: ignore  # noqa: E402

    print(json.dumps(seed_bootstrap_evaluation(store), indent=2))
