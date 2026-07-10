"""Structural collusion & Sybil detection for Agent Guild v0.2.

The detector is explainable, not a black box. It reads only the attestation
graph (aggregated, evidence-weighted edges) plus the EigenTrust vector and the
seed set, and returns a per-agent ``Flag`` carrying a ``suspicion`` in [0,1] and
human-readable ``reasons``.

Signals, all derived from the graph alone:

  * Mutual-high rings   — connected components in the "both directions rate each
                          other >= 0.7" graph. Reciprocal mutual praise is the
                          signature of a collusion ring.
  * Inward share        — fraction of a ring's outgoing endorsement weight that
                          stays inside the ring (an inward-looking clique).
  * Inflation           — how far ring members rate each other above the outside
                          consensus for those same members.
  * External validation — how many reviewers OUTSIDE the ring vouch for its
                          members (few = manufactured standing).
  * Distance from seeds — average EigenTrust of the ring vs the network max; a
                          ring with no seed inflow has earned nothing.
  * Lone-Sybil          — a subject whose incoming attestations all come from a
                          single reviewer (a one-account farm).
  * Reciprocity density — share of an agent's relationships that are reciprocal
                          mutual praise.

A ring that contains a pre-trusted seed has its suspicion strongly down-weighted
— a seed vouching from inside is strong evidence of genuine collaboration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


HIGH = 0.7  # rating threshold that counts as "praise" for ring detection


@dataclass
class Flag:
    agent_id: str
    suspicion: float = 0.0
    reasons: list[str] = field(default_factory=list)
    cluster_id: Optional[int] = None


def _components(adj: dict[str, set[str]]) -> list[set[str]]:
    seen: set[str] = set()
    comps: list[set[str]] = []
    for node in adj:
        if node in seen:
            continue
        stack = [node]
        comp: set[str] = set()
        while stack:
            cur = stack.pop()
            if cur in comp:
                continue
            comp.add(cur)
            seen.add(cur)
            for nb in adj.get(cur, ()):  # neighbours
                if nb not in comp:
                    stack.append(nb)
        if len(comp) >= 2:
            comps.append(comp)
    return comps


def detect_collusion(
    ids: list[str],
    out: dict[str, dict[str, "object"]],   # out[i][j] -> Pair(rating, weighted_count, ...)
    inc: dict[str, dict[str, "object"]],   # inc[j][i] -> Pair
    eigen: dict[str, float],
    seeds: set[str],
    *,
    high: float = HIGH,
) -> dict[str, Flag]:
    flags: dict[str, Flag] = {a: Flag(agent_id=a) for a in ids}
    max_eigen = max(eigen.values(), default=1e-12) or 1e-12

    # --- mutual-high-endorsement undirected graph ---------------------------
    mutual: dict[str, set[str]] = {a: set() for a in ids}
    for i in ids:
        for j, e_ij in out.get(i, {}).items():
            e_ji = out.get(j, {}).get(i)
            if e_ji is None:
                continue
            if e_ij.rating >= high and e_ji.rating >= high:
                mutual[i].add(j)
                mutual[j].add(i)

    rings = _components(mutual)

    # --- per-agent reciprocity density (independent of ring membership) -----
    for a in ids:
        partners = set(out.get(a, {})) | set(inc.get(a, {}))
        if partners:
            density = len(mutual[a]) / len(partners)
            if density >= 0.6 and len(mutual[a]) >= 2:
                flags[a].suspicion = max(flags[a].suspicion, 0.25 + 0.4 * density)
                flags[a].reasons.append(
                    f"{len(mutual[a])}/{len(partners)} relationships are reciprocal mutual praise"
                )

    # --- ring-level analysis ------------------------------------------------
    for cid, ring in enumerate(rings):
        ring_set = set(ring)
        contains_seed = bool(ring_set & seeds)

        # inward share: outgoing weight that stays inside the ring
        inside = outside = 0.0
        for i in ring_set:
            for j, e in out.get(i, {}).items():
                w = max(0.0, e.rating) * e.weighted_count
                if j in ring_set:
                    inside += w
                else:
                    outside += w
        inward_share = inside / (inside + outside) if (inside + outside) > 0 else 0.0

        # inflation: ring-internal rating of members vs external consensus
        infl_terms = []
        external_reviewers: set[str] = set()
        for m in ring_set:
            ext_ratings = [
                e.rating for r, e in inc.get(m, {}).items() if r not in ring_set
            ]
            for r in inc.get(m, {}):
                if r not in ring_set:
                    external_reviewers.add(r)
            int_ratings = [
                e.rating for r, e in inc.get(m, {}).items() if r in ring_set
            ]
            if int_ratings:
                ext_mean = sum(ext_ratings) / len(ext_ratings) if ext_ratings else 0.3
                infl_terms.append(sum(int_ratings) / len(int_ratings) - ext_mean)
        inflation = max(0.0, sum(infl_terms) / len(infl_terms)) if infl_terms else 0.0

        # external validation: outside reviewers per ring member
        ext_per_member = len(external_reviewers) / len(ring_set)

        # distance from seeds: ring's standing relative to network max
        ring_eigen = sum(eigen.get(m, 0.0) for m in ring_set) / len(ring_set)
        closeness = ring_eigen / max_eigen  # 1 = as trusted as the top agent

        # combine into a bounded suspicion
        suspicion = (
            0.45 * inward_share
            + 0.30 * min(1.0, inflation / 0.5)
            + 0.25 * (1.0 - min(1.0, ext_per_member))
        )
        # Earned standing lowers suspicion — but only when that standing was
        # earned OUTSIDE the ring. With zero external reviewers the ring's
        # EigenTrust mass is self-generated (in a tiny/young network the ring
        # can even BE the network max), so discounting by closeness would let
        # a pure two-account farm wash itself clean.
        if external_reviewers:
            suspicion *= 1.0 - min(0.9, closeness)    # externally-earned standing lowers it
        if contains_seed:
            suspicion *= 0.15                          # seed inside ⇒ likely real
        suspicion = max(0.0, min(1.0, suspicion))

        reasons = []
        if inward_share >= 0.5:
            reasons.append(f"{inward_share*100:.0f}% of endorsements stay inside the cluster")
        if inflation > 0.1:
            reasons.append(f"rates own members +{inflation:.2f} above outside consensus")
        if ext_per_member < 1.0:
            reasons.append(f"only {ext_per_member:.1f} external reviewers per member")
        if closeness < 0.15:
            reasons.append("no trust path from any seed (manufactured standing)")
        if contains_seed:
            reasons.append("contains a pre-trusted seed — down-weighted as likely genuine")

        for m in ring_set:
            f = flags[m]
            if suspicion >= f.suspicion:
                f.suspicion = suspicion
                f.cluster_id = cid
                f.reasons = list(reasons)

    # --- lone-Sybil signal --------------------------------------------------
    for a in ids:
        reviewers = list(inc.get(a, {}).keys())
        if len(reviewers) == 1 and a not in seeds:
            lone = reviewers[0]
            # a real reviewer with broad standing vouching once is fine; a single
            # *untrusted* reviewer that is the agent's ONLY source is a farm tell.
            if eigen.get(lone, 0.0) / max_eigen < 0.2:
                f = flags[a]
                f.suspicion = max(f.suspicion, 0.5)
                f.reasons.append("all incoming attestations come from a single untrusted reviewer")

    # --- uniform-farm Sybil signal ------------------------------------------
    # A boosted target need not rate anyone back (so it escapes ring detection),
    # but a wall of praise from many fresh, zero-trust accounts giving the SAME
    # near-perfect score is the signature of a Sybil farm. Genuine newcomers have
    # at least one trusted reviewer and/or rating variance, so they are spared.
    for a in ids:
        if a in seeds:
            continue
        revs = inc.get(a, {})
        if len(revs) < 3:
            continue
        ratings = [e.rating for e in revs.values()]
        all_untrusted = all(eigen.get(rv, 0.0) / max_eigen < 0.12 for rv in revs)
        mean_r = sum(ratings) / len(ratings)
        var = sum((x - mean_r) ** 2 for x in ratings) / len(ratings)
        uniform_farm = all_untrusted and mean_r >= 0.85 and var < 0.02
        # a jittered / larger farm still has the tell of *many* zero-trust praisers
        broad_farm = all_untrusted and len(revs) >= 5 and mean_r >= 0.75
        if uniform_farm or broad_farm:
            f = flags[a]
            f.suspicion = max(f.suspicion, 0.6)
            f.reasons.append(
                f"{len(revs)} reviewers, all zero-trust, mean {mean_r:.2f} ratings — Sybil-boosted"
            )

    return flags
