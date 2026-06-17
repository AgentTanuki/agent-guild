// ---------------------------------------------------------------------------
// Sybil / collusion detection.
// Structural, explainable heuristics over the attestation graph. The goal is
// not a perfect classifier but transparent, human-readable warnings.
// ---------------------------------------------------------------------------
import type { Agent, CollusionFlag } from "./types";
import type { AggregatedGraph } from "./graph";

const HIGH = 0.7; // threshold for a "strong" endorsement

interface Cluster {
  id: number;
  members: Set<string>;
}

/** Connected components over the mutual-high-endorsement graph (i↔j both ≥HIGH). */
function findRings(graph: AggregatedGraph): Cluster[] {
  const adj = new Map<string, Set<string>>();
  for (const id of graph.agentIds) adj.set(id, new Set());
  for (const e of graph.edges.values()) {
    const back = graph.edges.get(`${e.to}->${e.from}`);
    if (e.rating >= HIGH && back && back.rating >= HIGH) {
      adj.get(e.from)!.add(e.to);
      adj.get(e.to)!.add(e.from);
    }
  }
  const seen = new Set<string>();
  const clusters: Cluster[] = [];
  let cid = 0;
  for (const id of graph.agentIds) {
    if (seen.has(id) || adj.get(id)!.size === 0) continue;
    const members = new Set<string>();
    const stack = [id];
    while (stack.length) {
      const cur = stack.pop()!;
      if (members.has(cur)) continue;
      members.add(cur);
      seen.add(cur);
      for (const nb of adj.get(cur)!) if (!members.has(nb)) stack.push(nb);
    }
    if (members.size >= 2) clusters.push({ id: cid++, members });
  }
  return clusters;
}

export interface CollusionParams {
  /** Reciprocal cluster internal-share above which we get suspicious. */
  inwardThreshold: number;
}

export const DEFAULT_COLLUSION_PARAMS: CollusionParams = { inwardThreshold: 0.5 };

/**
 * Detect collusion and Sybil patterns.
 * @param consensusQuality reviewer-weighted consensus rating per agent, [0,1].
 * @param eigenTrust recursive global trust per agent (small positive numbers).
 */
export function detectCollusion(
  agents: Agent[],
  graph: AggregatedGraph,
  seedIds: Set<string>,
  consensusQuality: Map<string, number>,
  eigenTrust: Map<string, number>,
  params: CollusionParams = DEFAULT_COLLUSION_PARAMS,
): Map<string, CollusionFlag> {
  const flags = new Map<string, CollusionFlag>();
  for (const a of agents) {
    flags.set(a.id, { agentId: a.id, suspicion: 0, reasons: [], clusterId: undefined });
  }

  const rings = findRings(graph);
  const maxEigen = Math.max(...eigenTrust.values(), 1e-9);

  for (const ring of rings) {
    const members = [...ring.members];
    const containsSeed = members.some((m) => seedIds.has(m));

    // Fraction of members' outgoing endorsement weight aimed inside the ring.
    let inward = 0;
    let total = 0;
    for (const m of members) {
      for (const e of graph.out.get(m) ?? []) {
        total += e.rating * e.count;
        if (ring.members.has(e.to)) inward += e.rating * e.count;
      }
    }
    const inwardShare = total > 0 ? inward / total : 0;

    // Inflation: how far ring members rate each other above outside consensus.
    let inflation = 0;
    let infN = 0;
    for (const m of members) {
      for (const e of graph.out.get(m) ?? []) {
        if (ring.members.has(e.to)) {
          inflation += e.rating - (consensusQuality.get(e.to) ?? 0.5);
          infN += 1;
        }
      }
    }
    const avgInflation = infN > 0 ? inflation / infN : 0;

    // External trust inflow: do non-ring, non-trivial agents vouch for members?
    const externalReviewers = new Set<string>();
    for (const m of members) {
      for (const e of graph.in.get(m) ?? []) {
        if (!ring.members.has(e.from)) externalReviewers.add(e.from);
      }
    }

    // Average recursive trust of the ring relative to the network max.
    const avgEigen =
      members.reduce((s, m) => s + (eigenTrust.get(m) ?? 0), 0) / members.length / maxEigen;

    // Build suspicion from explainable components.
    let suspicion = 0;
    const reasons: string[] = [];
    if (inwardShare >= params.inwardThreshold) {
      suspicion += 0.35 * ((inwardShare - params.inwardThreshold) / (1 - params.inwardThreshold));
      reasons.push(
        `${members.length}-agent reciprocal ring directs ${(inwardShare * 100).toFixed(0)}% of its endorsements inward.`,
      );
    }
    if (avgInflation > 0.1) {
      suspicion += Math.min(0.3, avgInflation);
      reasons.push(
        `Ring rates its own members +${(avgInflation * 100).toFixed(0)}% above outside consensus.`,
      );
    }
    if (externalReviewers.size < members.length) {
      suspicion += 0.2;
      reasons.push(
        `Only ${externalReviewers.size} external reviewer(s) for a ${members.length}-agent ring (low outside validation).`,
      );
    }
    if (!containsSeed && avgEigen < 0.25) {
      suspicion += 0.15;
      reasons.push(`Ring is far from any pre-trusted seed (low recursive trust inflow).`);
    }
    if (containsSeed) {
      suspicion *= 0.3; // a seed in the ring is strong evidence against collusion
      reasons.push(`Contains a pre-trusted seed — suspicion downweighted.`);
    }

    suspicion = Math.max(0, Math.min(1, suspicion));
    for (const m of members) {
      const f = flags.get(m)!;
      f.suspicion = suspicion;
      f.reasons = reasons.slice();
      f.clusterId = ring.id;
    }
  }

  // Lone Sybil signal: an agent reviewed by nobody outside a single source.
  for (const a of agents) {
    const f = flags.get(a.id)!;
    if (f.suspicion > 0) continue;
    const reviewers = new Set((graph.in.get(a.id) ?? []).map((e) => e.from));
    if (reviewers.size === 1 && (graph.in.get(a.id)?.length ?? 0) >= 2) {
      f.suspicion = 0.3;
      f.reasons.push(`All incoming attestations come from a single reviewer (possible Sybil farm).`);
    }
  }

  return flags;
}

export { findRings };
