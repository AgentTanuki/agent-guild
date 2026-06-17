// ---------------------------------------------------------------------------
// Shared graph helpers: aggregate verified attestations into edges.
// ---------------------------------------------------------------------------
import type { Agent, Attestation } from "./types";

export interface Edge {
  from: string; // reviewer id
  to: string; // subject id
  /** Mean rating in [0,1]. */
  rating: number;
  count: number;
}

export interface AggregatedGraph {
  agentIds: string[];
  index: Map<string, number>;
  /** edges keyed by `${from}->${to}`. */
  edges: Map<string, Edge>;
  /** outgoing edges per agent id. */
  out: Map<string, Edge[]>;
  /** incoming edges per agent id. */
  in: Map<string, Edge[]>;
}

export function buildGraph(agents: Agent[], attestations: Attestation[]): AggregatedGraph {
  const agentIds = agents.map((a) => a.id);
  const index = new Map(agentIds.map((id, i) => [id, i]));
  const acc = new Map<string, { sum: number; count: number; from: string; to: string }>();

  for (const a of attestations) {
    if (!a.verified) continue; // only cryptographically valid attestations count
    if (a.reviewerId === a.subjectId) continue; // no self-review
    if (!index.has(a.reviewerId) || !index.has(a.subjectId)) continue;
    const key = `${a.reviewerId}->${a.subjectId}`;
    const cur = acc.get(key) ?? { sum: 0, count: 0, from: a.reviewerId, to: a.subjectId };
    cur.sum += a.rating;
    cur.count += 1;
    acc.set(key, cur);
  }

  const edges = new Map<string, Edge>();
  const out = new Map<string, Edge[]>();
  const inc = new Map<string, Edge[]>();
  for (const id of agentIds) {
    out.set(id, []);
    inc.set(id, []);
  }
  for (const [key, v] of acc) {
    const edge: Edge = { from: v.from, to: v.to, rating: v.sum / v.count, count: v.count };
    edges.set(key, edge);
    out.get(v.from)!.push(edge);
    inc.get(v.to)!.push(edge);
  }
  return { agentIds, index, edges, out, in: inc };
}
