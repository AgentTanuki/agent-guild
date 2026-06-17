// ---------------------------------------------------------------------------
// Reputation scoring — the heart of Agent Guild.
//
// Pipeline:
//   1. Aggregate verified attestations into a weighted trust graph.
//   2. EigenTrust: recursive global trust seeded from pre-trusted agents.
//      Trust only flows from the seeds, so isolated Sybil/collusion clusters
//      cannot manufacture standing.
//   3. Reviewer-weighted consensus quality: what trusted reviewers think of you.
//   4. Endorsement accuracy: reviewers whose ratings disagree with consensus
//      lose standing (penalises rubber-stamping ring-mates / bad endorsements).
//   5. Collusion penalty from the structural detector.
//   6. Confidence shrinkage: thin evidence (newcomers, Sybils) is pulled toward
//      a low prior, so reputation must be *earned* with diverse attestations.
// ---------------------------------------------------------------------------
import type { Agent, Attestation, ReputationScore, CollusionFlag } from "./types";
import { buildGraph, type AggregatedGraph } from "./graph";
import { detectCollusion } from "./collusion";

export interface ScoringParams {
  /** EigenTrust teleport-to-seed probability (Sybil-resistance knob). */
  alpha: number;
  iterations: number;
  /** Blend between recursive trust and absolute consensus quality. */
  eigenWeight: number;
  /** Low prior that thin-evidence agents are shrunk toward, in [0,1]. */
  prior: number;
  /** Evidence (distinct reviewers) at which confidence ≈ 0.63. */
  confidenceK: number;
  /** Weight of endorsement-accuracy penalty. */
  endorsementWeight: number;
}

export const DEFAULT_PARAMS: ScoringParams = {
  alpha: 0.2,
  iterations: 60,
  eigenWeight: 0.5,
  prior: 0.2,
  confidenceK: 3,
  endorsementWeight: 0.3,
};

export interface ScoringResult {
  scores: Map<string, ReputationScore>;
  flags: Map<string, CollusionFlag>;
  graph: AggregatedGraph;
  consensusQuality: Map<string, number>;
  eigenTrust: Map<string, number>;
}

export function scoreAgents(
  agents: Agent[],
  attestations: Attestation[],
  seedAgentIds: string[],
  params: ScoringParams = DEFAULT_PARAMS,
): ScoringResult {
  const graph = buildGraph(agents, attestations);
  const ids = graph.agentIds;
  const n = ids.length;
  const seedSet = new Set(seedAgentIds.filter((id) => graph.index.has(id)));

  // --- 1+2. EigenTrust ------------------------------------------------------
  // Local trust C: row-normalised positive endorsement weights.
  const C = new Map<string, Map<string, number>>();
  for (const id of ids) {
    const row = new Map<string, number>();
    let sum = 0;
    for (const e of graph.out.get(id) ?? []) {
      const w = Math.max(0, e.rating) * e.count;
      if (w > 0) {
        row.set(e.to, w);
        sum += w;
      }
    }
    if (sum > 0) for (const [k, v] of row) row.set(k, v / sum);
    C.set(id, row);
  }

  // Pre-trust distribution p over seeds (uniform); fallback uniform over all.
  const p = new Map<string, number>();
  if (seedSet.size > 0) {
    for (const id of ids) p.set(id, seedSet.has(id) ? 1 / seedSet.size : 0);
  } else {
    for (const id of ids) p.set(id, 1 / n);
  }

  // Power iteration: t = (1-alpha) * Cᵀ t + alpha * p
  let t = new Map<string, number>(ids.map((id) => [id, p.get(id)!]));
  for (let iter = 0; iter < params.iterations; iter++) {
    const next = new Map<string, number>(ids.map((id) => [id, params.alpha * p.get(id)!]));
    let dangling = 0;
    for (const id of ids) {
      const row = C.get(id)!;
      const ti = t.get(id)!;
      if (row.size === 0) {
        dangling += ti; // agents who endorse no one redistribute via pre-trust
        continue;
      }
      for (const [j, w] of row) {
        next.set(j, next.get(j)! + (1 - params.alpha) * ti * w);
      }
    }
    // Redistribute dangling mass over the pre-trust set.
    for (const id of ids) {
      next.set(id, next.get(id)! + (1 - params.alpha) * dangling * p.get(id)!);
    }
    t = next;
  }
  const eigenTrust = t;
  const maxEigen = Math.max(...eigenTrust.values(), 1e-12);

  // --- 3. Reviewer-weighted consensus quality -------------------------------
  // Each incoming rating weighted by the reviewer's recursive trust.
  const consensusQuality = new Map<string, number>();
  for (const id of ids) {
    let num = 0;
    let den = 0;
    for (const e of graph.in.get(id) ?? []) {
      const w = (eigenTrust.get(e.from) ?? 0) + 1e-6; // tiny floor so any signal counts
      num += w * e.rating * e.count;
      den += w * e.count;
    }
    consensusQuality.set(id, den > 0 ? num / den : params.prior);
  }

  // --- 4. Endorsement accuracy ---------------------------------------------
  // How closely each reviewer's given ratings track eventual consensus.
  const endorsementAccuracy = new Map<string, number>();
  for (const id of ids) {
    let err = 0;
    let cnt = 0;
    for (const e of graph.out.get(id) ?? []) {
      err += Math.abs(e.rating - (consensusQuality.get(e.to) ?? params.prior)) * e.count;
      cnt += e.count;
    }
    endorsementAccuracy.set(id, cnt > 0 ? 1 - err / cnt : 1); // no reviews ⇒ neutral
  }

  // --- 5. Collusion penalty -------------------------------------------------
  const flags = detectCollusion(agents, graph, seedSet, consensusQuality, eigenTrust);

  // --- 6. Compose final trust ----------------------------------------------
  const raw = new Map<string, number>();
  for (const id of ids) {
    const eigenScaled = eigenTrust.get(id)! / maxEigen; // [0,1] relative standing
    const quality = consensusQuality.get(id)!; // [0,1] absolute
    let base = params.eigenWeight * eigenScaled + (1 - params.eigenWeight) * quality;

    // Endorsement penalty: bad endorsers lose standing.
    const acc = endorsementAccuracy.get(id)!;
    base *= 1 - params.endorsementWeight * (1 - acc);

    // Collusion penalty.
    const suspicion = flags.get(id)?.suspicion ?? 0;
    base *= 1 - suspicion;

    // Confidence from diversity of evidence.
    const reviewers = new Set((graph.in.get(id) ?? []).map((e) => e.from));
    const confidence = 1 - Math.exp(-reviewers.size / params.confidenceK);
    const shrunk = confidence * base + (1 - confidence) * params.prior;

    raw.set(id, shrunk);
  }

  // Rank and package.
  const ordered = [...ids].sort((a, b) => raw.get(b)! - raw.get(a)!);
  const rankOf = new Map<string, number>(ordered.map((id, i) => [id, i + 1]));

  const scores = new Map<string, ReputationScore>();
  for (const id of ids) {
    const reviewers = new Set((graph.in.get(id) ?? []).map((e) => e.from));
    const confidence = 1 - Math.exp(-reviewers.size / params.confidenceK);
    scores.set(id, {
      agentId: id,
      trust: Math.round(raw.get(id)! * 1000) / 10, // 0..100, 1 dp
      eigenTrust: eigenTrust.get(id)!,
      weightedQuality: consensusQuality.get(id)!,
      endorsementAccuracy: endorsementAccuracy.get(id)!,
      collusionPenalty: flags.get(id)?.suspicion ?? 0,
      confidence,
      rank: rankOf.get(id)!,
    });
  }

  return { scores, flags, graph, consensusQuality, eigenTrust };
}
