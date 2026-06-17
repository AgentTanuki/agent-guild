import { useMemo, useState } from "react";
import {
  forceSimulation,
  forceLink,
  forceManyBody,
  forceCenter,
  forceCollide,
  type SimulationNodeDatum,
} from "d3-force";
import { useGuild } from "../lib/store";
import { ARCHETYPE_COLOR, ARCHETYPE_LABEL, trustColor } from "../ui/format";
import type { Archetype } from "../lib/types";

interface Node extends SimulationNodeDatum {
  id: string;
  handle: string;
  archetype: Archetype;
  trust: number;
  suspicion: number;
  clusterId?: number;
  seed: boolean;
}
interface Link {
  source: string | Node;
  target: string | Node;
  rating: number;
  intraCluster: boolean;
}

const W = 820;
const H = 560;

/** Force-directed trust graph. Edges are aggregated attestations; suspected
 *  collusion rings are drawn in amber, flagged nodes get a red halo. */
export function TrustGraph() {
  const { guild, derived, selectedId, select } = useGuild();
  const [colorMode, setColorMode] = useState<"archetype" | "trust">("archetype");

  const { nodes, links } = useMemo(() => {
    const nodes: Node[] = guild.agents.map((a) => {
      const s = derived.scores.get(a.id)!;
      const f = derived.flags.get(a.id);
      return {
        id: a.id,
        handle: a.handle,
        archetype: a.archetype,
        trust: s.trust,
        suspicion: f?.suspicion ?? 0,
        clusterId: f?.clusterId,
        seed: guild.seedAgentIds.includes(a.id),
      };
    });
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const links: Link[] = [];
    for (const e of derived.graph.edges.values()) {
      const a = byId.get(e.from);
      const b = byId.get(e.to);
      if (!a || !b) continue;
      links.push({
        source: e.from,
        target: e.to,
        rating: e.rating,
        intraCluster:
          a.clusterId !== undefined && a.clusterId === b.clusterId,
      });
    }
    const sim = forceSimulation(nodes)
      .force("link", forceLink<Node, Link>(links).id((d) => d.id).distance(60).strength(0.25))
      .force("charge", forceManyBody().strength(-120))
      .force("center", forceCenter(W / 2, H / 2))
      .force("collide", forceCollide<Node>().radius((d) => 6 + d.trust / 10))
      .stop();
    for (let i = 0; i < 320; i++) sim.tick();
    // Clamp into the viewport.
    for (const n of nodes) {
      n.x = Math.max(20, Math.min(W - 20, n.x ?? W / 2));
      n.y = Math.max(20, Math.min(H - 20, n.y ?? H / 2));
    }
    return { nodes, links };
  }, [guild, derived]);

  const radius = (n: Node) => 5 + n.trust / 9;
  const fill = (n: Node) => (colorMode === "archetype" ? ARCHETYPE_COLOR[n.archetype] : trustColor(n.trust));

  return (
    <div className="panel">
      <h2 style={{ display: "flex", justifyContent: "space-between" }}>
        <span>Trust graph</span>
        <span>
          <button onClick={() => setColorMode(colorMode === "archetype" ? "trust" : "archetype")}>
            Colour: {colorMode === "archetype" ? "archetype" : "trust score"}
          </button>
        </span>
      </h2>
      <svg className="graph" viewBox={`0 0 ${W} ${H}`}>
        {links.map((l, i) => {
          const s = l.source as Node;
          const t = l.target as Node;
          return (
            <line
              key={i}
              x1={s.x}
              y1={s.y}
              x2={t.x}
              y2={t.y}
              stroke={l.intraCluster ? "#fbbf24" : "#2c3b52"}
              strokeOpacity={l.intraCluster ? 0.7 : 0.25 + l.rating * 0.3}
              strokeWidth={l.intraCluster ? 1.6 : 1}
            />
          );
        })}
        {nodes.map((n) => (
          <g
            key={n.id}
            transform={`translate(${n.x},${n.y})`}
            style={{ cursor: "pointer" }}
            onClick={() => select(n.id)}
          >
            {n.suspicion > 0.4 && (
              <circle r={radius(n) + 5} fill="none" stroke="#f87171" strokeWidth={2} strokeDasharray="3 2" />
            )}
            {n.seed && <circle r={radius(n) + 3} fill="none" stroke="#f5c451" strokeWidth={2} />}
            <circle
              r={radius(n)}
              fill={fill(n)}
              stroke={selectedId === n.id ? "#fff" : "#0b0e14"}
              strokeWidth={selectedId === n.id ? 2.5 : 1}
            />
            {(selectedId === n.id || n.seed || n.trust > 60) && (
              <text x={radius(n) + 4} y={4} fontSize={10} fill="#e6e9ef">
                {n.handle}
              </text>
            )}
          </g>
        ))}
      </svg>
      <div className="legend">
        {(Object.keys(ARCHETYPE_LABEL) as Archetype[]).map((a) => (
          <span key={a}>
            <i className="dot" style={{ background: ARCHETYPE_COLOR[a] }} />
            {ARCHETYPE_LABEL[a]}
          </span>
        ))}
        <span><i className="dot" style={{ background: "#f5c451" }} /> gold ring = seed</span>
        <span><i className="dot" style={{ background: "#f87171" }} /> dashed halo = flagged</span>
        <span><i className="dot" style={{ background: "#fbbf24" }} /> amber edge = ring link</span>
      </div>
    </div>
  );
}
