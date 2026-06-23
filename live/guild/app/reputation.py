"""Reputation scoring for the live Guild — v0.2, "costly attestations".

The thesis of v0.2: an attestation should only *materially* move reputation if
it is backed by evidence of a real transaction. A signed message is just an
assertion; what makes it expensive to fake is a task receipt, a payment, and a
stake the issuer can lose. The engine therefore scores **evidence-weighted**
attestations and layers four defences on top of EigenTrust:

  1. Evidence weighting   — each attestation carries a weight in [0,1] from its
                            receipt / payment / stake backing (computed upstream
                            in the store). Unbacked praise barely counts.
  2. Anti-collusion       — structural ring/Sybil detection (collusion.py) plus
                            per-issuer and per-cluster caps on how much a single
                            source can lift a subject, and a trusted-diversity
                            confidence so a farm of fresh accounts earns nothing.
  3. Staking / slashing   — an issuer that stakes reputation on an attestation
                            which trusted consensus later contradicts is slashed,
                            and the slash is designed to exceed the subject's gain.
  4. Confidence shrinkage — thin or untrusted evidence stays near a low prior.

The engine never sees ground truth — only what agents have signed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .collusion import detect_collusion, Flag


@dataclass
class ScoringParams:
    alpha: float = 0.2              # EigenTrust teleport-to-seed probability
    iterations: int = 60
    eigen_weight: float = 0.5       # blend recursive trust vs absolute quality
    prior: float = 0.2              # low prior thin-evidence agents shrink toward
    confidence_k: float = 3.0       # distinct *trusted* reviewers for conf ~0.63
    endorsement_weight: float = 0.3
    trusted_eigen_frac: float = 0.12  # reviewer counts as "trusted" above this *max
    per_issuer_cap: float = 0.5     # max share of a subject's quality from one issuer
    per_cluster_cap: float = 0.6    # max share of a subject's quality from one cluster
    per_issuer_eigen_cap: float = 0.5  # max single C[i][j] (caps eigen pumping)
    slash_threshold: float = 0.30   # rating-vs-consensus gap that triggers slashing
    slash_coeff: float = 1.6        # converts staked deviation into a score penalty
    # --- absolute trust floor (hardening) ----------------------------------
    # A reviewer's vote only counts as "trusted" if its standing clears BOTH a
    # relative bar (trusted_eigen_frac × max) AND an absolute floor — and, when
    # the graph has seeds, the reviewer must be reachable from a seed along the
    # trust graph. This closes the documented hole where "trusted" was purely
    # relative to the network max and could be cleared cheaply in a near-empty
    # graph: a clique with no seed-anchored inflow now contributes ~no trusted
    # reviewers no matter how loudly it praises itself.
    abs_eigen_floor: float = 1e-4   # absolute minimum eigen to be a trusted reviewer
    require_seed_path: bool = True  # trusted reviewers must be seed-reachable (if seeds exist)
    # An agent with NO seed-anchored trusted support cannot bootstrap meaningful
    # trust: its score is hard-capped here (×100 = trust points) no matter how
    # much unanchored praise it accumulates. This is the absolute reputation
    # floor — unknown/island agents stay near zero instead of resting at the
    # prior, so reputation must be *earned* from seed-traceable evidence.
    unknown_trust_ceiling: float = 0.05


@dataclass
class AttRecord:
    """One attestation as the scorer sees it."""
    reviewer: str
    subject: str
    rating: float
    weight: float = 1.0   # evidence weight in [0,1] (receipt/payment/stake backing)
    stake: float = 0.0    # simulated reputation the reviewer staked on this claim


@dataclass
class Pair:
    rating: float          # evidence-weighted mean rating reviewer->subject
    weighted_count: float  # sum of evidence weights
    raw_count: int         # number of attestations
    stake: float           # total stake on this relationship


@dataclass
class AgentScore:
    agent_id: str
    trust: float                 # 0..100, the headline number
    eigen_trust: float
    weighted_quality: float
    endorsement_accuracy: float
    confidence: float
    distinct_reviewers: int
    attestations_received: int
    # --- v0.2 evidence breakdown -------------------------------------------
    raw_rating: float = 0.0          # unweighted mean of ratings received
    verified_task_count: int = 0     # receipts where this agent delivered
    trusted_attestations: int = 0    # received from trusted (high-eigen) issuers
    suspicious_attestations: int = 0 # received from flagged issuers
    backed_attestations: int = 0     # received that reference a real task receipt
    collusion_suspicion: float = 0.0
    slash_penalty: float = 0.0
    flag_reasons: list[str] = field(default_factory=list)
    rank: int = 0


@dataclass
class ScoringResult:
    scores: dict[str, AgentScore] = field(default_factory=dict)
    flags: dict[str, Flag] = field(default_factory=dict)


def _aggregate(
    agent_ids: list[str],
    records: list[AttRecord],
) -> tuple[dict[str, dict[str, Pair]], dict[str, dict[str, Pair]]]:
    valid = set(agent_ids)
    acc: dict[tuple[str, str], list[AttRecord]] = {}
    for r in records:
        if r.reviewer == r.subject or r.reviewer not in valid or r.subject not in valid:
            continue
        acc.setdefault((r.reviewer, r.subject), []).append(r)
    out: dict[str, dict[str, Pair]] = {a: {} for a in agent_ids}
    inc: dict[str, dict[str, Pair]] = {a: {} for a in agent_ids}
    for (rv, sub), rs in acc.items():
        wsum = sum(max(0.0, x.weight) for x in rs)
        if wsum > 0:
            rating = sum(x.rating * max(0.0, x.weight) for x in rs) / wsum
        else:
            rating = sum(x.rating for x in rs) / len(rs)
        pair = Pair(
            rating=rating,
            weighted_count=wsum,
            raw_count=len(rs),
            stake=sum(x.stake for x in rs),
        )
        out[rv][sub] = pair
        inc[sub][rv] = pair
    return out, inc


def score(
    agent_ids: list[str],
    records: Iterable[AttRecord],
    seed_ids: Iterable[str] | None = None,
    receipt_counts: Optional[dict[str, int]] = None,
    params: ScoringParams | None = None,
) -> ScoringResult:
    p = params or ScoringParams()
    records = list(records)
    ids = list(agent_ids)
    n = len(ids)
    if n == 0:
        return ScoringResult()
    seeds = set(s for s in (seed_ids or []) if s in set(ids))
    receipt_counts = receipt_counts or {}

    out, inc = _aggregate(ids, records)

    # --- Local trust matrix C: row-normalised, then per-entry capped. --------
    C: dict[str, dict[str, float]] = {}
    for a in ids:
        row: dict[str, float] = {}
        s = 0.0
        for sub, e in out[a].items():
            w = max(0.0, e.rating) * e.weighted_count
            if w > 0:
                row[sub] = w
                s += w
        if s > 0:
            row = {k: min(p.per_issuer_eigen_cap, v / s) for k, v in row.items()}
        C[a] = row

    # --- Pre-trust distribution over seeds (else uniform). -------------------
    pre: dict[str, float] = {}
    if seeds:
        for a in ids:
            pre[a] = 1.0 / len(seeds) if a in seeds else 0.0
    else:
        for a in ids:
            pre[a] = 1.0 / n

    # --- EigenTrust power iteration: t = (1-a) Cᵀ t + a p --------------------
    t = {a: pre[a] for a in ids}
    for _ in range(p.iterations):
        nxt = {a: p.alpha * pre[a] for a in ids}
        leak = 0.0  # mass lost to dangling rows and to per-issuer capping
        for a in ids:
            row = C[a]
            ta = t[a]
            routed = sum(row.values())
            for j, w in row.items():
                nxt[j] += (1 - p.alpha) * ta * w
            leak += (1 - p.alpha) * ta * (1.0 - routed)
        for a in ids:
            nxt[a] += leak * pre[a]
        t = nxt
    eigen = t
    max_eigen = max(eigen.values(), default=1e-12) or 1e-12

    # --- Seed reachability: which agents are reachable from a seed along the ---
    # trust graph (seed trusts X, X trusts Y, …). Trust in EigenTrust only has
    # standing if it traces back to a pre-trusted seed; an island clique is not
    # seed-reachable and therefore cannot host a "trusted" reviewer.
    seed_reachable: set[str] = set(seeds)
    if seeds:
        frontier = list(seeds)
        while frontier:
            a = frontier.pop()
            for sub, w in C.get(a, {}).items():
                if w > 0 and sub not in seed_reachable:
                    seed_reachable.add(sub)
                    frontier.append(sub)
    else:
        seed_reachable = set(ids)  # no seeds: fall back to old behaviour

    # --- Collusion / Sybil flags --------------------------------------------
    flags = detect_collusion(ids, out, inc, eigen, seeds, high=0.7)
    cluster_of: dict[str, Optional[int]] = {a: flags[a].cluster_id for a in ids}

    # --- Reviewer-weighted consensus quality, with per-issuer/cluster caps ---
    consensus: dict[str, float] = {}
    for a in ids:
        contribs: list[tuple[float, float, Optional[int]]] = []  # (weight, rating, cluster)
        for rv, e in inc[a].items():
            w = (eigen.get(rv, 0.0) + 1e-6) * e.weighted_count
            contribs.append((w, e.rating, cluster_of.get(rv)))
        total = sum(w for w, _, _ in contribs)
        if total <= 0:
            consensus[a] = p.prior
            continue
        # cap per single issuer
        cap_i = p.per_issuer_cap * total
        contribs = [(min(w, cap_i), r, c) for w, r, c in contribs]
        # cap per cluster
        by_cluster: dict[int, float] = {}
        for w, _, c in contribs:
            if c is not None:
                by_cluster[c] = by_cluster.get(c, 0.0) + w
        total2 = sum(w for w, _, _ in contribs)
        cap_c = p.per_cluster_cap * total2
        scale: dict[int, float] = {}
        for c, wsum in by_cluster.items():
            scale[c] = min(1.0, cap_c / wsum) if wsum > cap_c else 1.0
        num = den = 0.0
        for w, r, c in contribs:
            f = scale.get(c, 1.0) if c is not None else 1.0
            num += w * f * r
            den += w * f
        consensus[a] = (num / den) if den > 0 else p.prior

    # --- Endorsement accuracy (does the reviewer track consensus?) -----------
    endorsement: dict[str, float] = {}
    for a in ids:
        err = cnt = 0.0
        for sub, e in out[a].items():
            err += abs(e.rating - consensus.get(sub, p.prior)) * e.weighted_count
            cnt += e.weighted_count
        endorsement[a] = (1 - err / cnt) if cnt > 0 else 1.0

    # --- Staking / slashing: a stake on a claim trusted consensus rejects ----
    slash: dict[str, float] = {a: 0.0 for a in ids}
    for r in records:
        if r.stake <= 0 or r.reviewer not in C or r.subject not in consensus:
            continue
        dev = abs(r.rating - consensus[r.subject])
        if dev > p.slash_threshold:
            slash[r.reviewer] += r.stake * (dev - p.slash_threshold)
    slash_penalty = {a: min(0.9, p.slash_coeff * slash[a]) for a in ids}

    # --- Trusted reviewer predicate -----------------------------------------
    # A reviewer is trusted only if it clears the relative bar AND an absolute
    # floor AND (when seeds exist) is reachable from a seed. All three must hold,
    # so an island clique with no seed inflow earns no confidence for its targets
    # however much it inflates them.
    def trusted(rv: str) -> bool:
        e = eigen.get(rv, 0.0)
        if e < p.abs_eigen_floor:
            return False
        if e < p.trusted_eigen_frac * max_eigen:
            return False
        if p.require_seed_path and seeds and rv not in seed_reachable:
            return False
        return True

    # --- Compose final score ------------------------------------------------
    raw: dict[str, float] = {}
    trusted_reviewers_of: dict[str, int] = {}
    for a in ids:
        eigen_scaled = eigen[a] / max_eigen
        quality = consensus[a]
        base = p.eigen_weight * eigen_scaled + (1 - p.eigen_weight) * quality
        base *= 1 - p.endorsement_weight * (1 - endorsement[a])
        base *= 1 - flags[a].suspicion          # collusion multiplier
        base *= 1 - slash_penalty[a]            # staking slash
        # confidence from distinct *trusted* reviewers (a Sybil farm of fresh
        # accounts contributes ~0 trusted reviewers, so it cannot buy confidence)
        tr = sum(1 for rv in inc[a] if trusted(rv))
        trusted_reviewers_of[a] = tr
        confidence = 1 - math.exp(-tr / p.confidence_k)
        raw[a] = confidence * base + (1 - confidence) * p.prior
        # Absolute floor: an agent with no seed-anchored trusted support (and, if
        # seeds exist, not seed-reachable) cannot bootstrap trust above the
        # unknown ceiling — unanchored reputation is pinned near zero.
        anchored = (a in seeds) or tr > 0 or (bool(seeds) and a in seed_reachable)
        if not anchored:
            raw[a] = min(raw[a], p.unknown_trust_ceiling)

    ordered = sorted(ids, key=lambda a: raw[a], reverse=True)
    rank_of = {a: i + 1 for i, a in enumerate(ordered)}

    scores: dict[str, AgentScore] = {}
    for a in ids:
        reviewers = list(inc[a].keys())
        received = sum(e.raw_count for e in inc[a].values())
        raw_ratings = [e.rating for e in inc[a].values()]
        raw_rating = sum(raw_ratings) / len(raw_ratings) if raw_ratings else 0.0
        backed = sum(
            e.raw_count for e in inc[a].values()
            if e.weighted_count / max(1, e.raw_count) > 0.4
        )
        susp = sum(1 for rv in reviewers if flags[rv].suspicion >= 0.4)
        trust_cnt = trusted_reviewers_of[a]
        confidence = 1 - math.exp(-trust_cnt / p.confidence_k)
        scores[a] = AgentScore(
            agent_id=a,
            trust=round(raw[a] * 1000) / 10,
            eigen_trust=eigen[a],
            weighted_quality=consensus[a],
            endorsement_accuracy=endorsement[a],
            confidence=confidence,
            distinct_reviewers=len(reviewers),
            attestations_received=received,
            raw_rating=raw_rating,
            verified_task_count=receipt_counts.get(a, 0),
            trusted_attestations=trust_cnt,
            suspicious_attestations=susp,
            backed_attestations=backed,
            collusion_suspicion=flags[a].suspicion,
            slash_penalty=slash_penalty[a],
            flag_reasons=list(flags[a].reasons),
            rank=rank_of[a],
        )
    return ScoringResult(scores=scores, flags=flags)


# --------------------------------------------------------------------------- #
# Backward-compatible adapter (v0.1 callers passing bare (i, j, rating) edges). #
# Unbacked edges default to full weight so legacy behaviour is preserved.       #
# --------------------------------------------------------------------------- #
def score_agents(
    agent_ids: list[str],
    attestations: Iterable[tuple[str, str, float]],
    seed_ids: Iterable[str] | None = None,
    params: ScoringParams | None = None,
) -> ScoringResult:
    recs = [AttRecord(reviewer=i, subject=j, rating=float(r), weight=1.0)
            for (i, j, r) in attestations]
    return score(agent_ids, recs, seed_ids, params=params)
