"""Reputation scoring for the live Guild — the product.

EigenTrust-style recursive global trust seeded from pre-trusted agents, layered
with reviewer-weighted consensus quality, an endorsement-accuracy penalty, and
confidence shrinkage so thin evidence stays near a low prior. Ported from the
TypeScript prototype's reputation engine.

Inputs are deliberately minimal and identity-agnostic: a set of agent ids, a
list of (reviewer, subject, rating) attestations, and a seed set. The engine
never sees any "ground truth" about an agent — only what others have signed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class ScoringParams:
    alpha: float = 0.2          # EigenTrust teleport-to-seed probability
    iterations: int = 60
    eigen_weight: float = 0.5   # blend recursive trust vs absolute quality
    prior: float = 0.2          # low prior thin-evidence agents shrink toward
    confidence_k: float = 3.0   # distinct reviewers for confidence ~ 0.63
    endorsement_weight: float = 0.3


@dataclass
class Edge:
    rating: float
    count: int


@dataclass
class AgentScore:
    agent_id: str
    trust: float            # 0..100
    eigen_trust: float
    weighted_quality: float
    endorsement_accuracy: float
    confidence: float
    distinct_reviewers: int
    attestations_received: int
    rank: int = 0


@dataclass
class ScoringResult:
    scores: dict[str, AgentScore] = field(default_factory=dict)


def _aggregate(
    agent_ids: list[str],
    attestations: Iterable[tuple[str, str, float]],
) -> tuple[dict[str, dict[str, Edge]], dict[str, dict[str, Edge]]]:
    """Return (out_edges, in_edges) keyed by agent id, aggregating ratings."""
    valid = set(agent_ids)
    acc: dict[tuple[str, str], list[float]] = {}
    for reviewer, subject, rating in attestations:
        if reviewer == subject or reviewer not in valid or subject not in valid:
            continue
        acc.setdefault((reviewer, subject), []).append(float(rating))
    out: dict[str, dict[str, Edge]] = {a: {} for a in agent_ids}
    inc: dict[str, dict[str, Edge]] = {a: {} for a in agent_ids}
    for (reviewer, subject), ratings in acc.items():
        edge = Edge(rating=sum(ratings) / len(ratings), count=len(ratings))
        out[reviewer][subject] = edge
        inc[subject][reviewer] = edge
    return out, inc


def score_agents(
    agent_ids: list[str],
    attestations: Iterable[tuple[str, str, float]],
    seed_ids: Iterable[str] | None = None,
    params: ScoringParams | None = None,
) -> ScoringResult:
    p = params or ScoringParams()
    attestations = list(attestations)
    ids = list(agent_ids)
    n = len(ids)
    if n == 0:
        return ScoringResult()
    seeds = set(s for s in (seed_ids or []) if s in set(ids))

    out, inc = _aggregate(ids, attestations)

    # --- Local trust matrix C: row-normalised positive endorsement weight. ---
    C: dict[str, dict[str, float]] = {}
    for a in ids:
        row: dict[str, float] = {}
        s = 0.0
        for sub, e in out[a].items():
            w = max(0.0, e.rating) * e.count
            if w > 0:
                row[sub] = w
                s += w
        if s > 0:
            row = {k: v / s for k, v in row.items()}
        C[a] = row

    # --- Pre-trust distribution p over seeds (else uniform). -----------------
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
        dangling = 0.0
        for a in ids:
            row = C[a]
            ta = t[a]
            if not row:
                dangling += ta
                continue
            for j, w in row.items():
                nxt[j] += (1 - p.alpha) * ta * w
        for a in ids:
            nxt[a] += (1 - p.alpha) * dangling * pre[a]
        t = nxt
    eigen = t
    max_eigen = max(eigen.values()) if eigen else 1e-12
    max_eigen = max(max_eigen, 1e-12)

    # --- Reviewer-weighted consensus quality ---------------------------------
    consensus: dict[str, float] = {}
    for a in ids:
        num = den = 0.0
        for rv, e in inc[a].items():
            w = eigen.get(rv, 0.0) + 1e-6
            num += w * e.rating * e.count
            den += w * e.count
        consensus[a] = (num / den) if den > 0 else p.prior

    # --- Endorsement accuracy ------------------------------------------------
    endorsement: dict[str, float] = {}
    for a in ids:
        err = cnt = 0.0
        for sub, e in out[a].items():
            err += abs(e.rating - consensus.get(sub, p.prior)) * e.count
            cnt += e.count
        endorsement[a] = (1 - err / cnt) if cnt > 0 else 1.0

    # --- Compose -------------------------------------------------------------
    raw: dict[str, float] = {}
    for a in ids:
        eigen_scaled = eigen[a] / max_eigen
        quality = consensus[a]
        base = p.eigen_weight * eigen_scaled + (1 - p.eigen_weight) * quality
        base *= 1 - p.endorsement_weight * (1 - endorsement[a])
        reviewers = len(inc[a])
        confidence = 1 - math.exp(-reviewers / p.confidence_k)
        raw[a] = confidence * base + (1 - confidence) * p.prior

    ordered = sorted(ids, key=lambda a: raw[a], reverse=True)
    rank_of = {a: i + 1 for i, a in enumerate(ordered)}

    scores: dict[str, AgentScore] = {}
    for a in ids:
        reviewers = len(inc[a])
        confidence = 1 - math.exp(-reviewers / p.confidence_k)
        received = sum(e.count for e in inc[a].values())
        scores[a] = AgentScore(
            agent_id=a,
            trust=round(raw[a] * 1000) / 10,
            eigen_trust=eigen[a],
            weighted_quality=consensus[a],
            endorsement_accuracy=endorsement[a],
            confidence=confidence,
            distinct_reviewers=reviewers,
            attestations_received=received,
            rank=rank_of[a],
        )
    return ScoringResult(scores=scores)
