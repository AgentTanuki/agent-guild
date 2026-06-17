// ---------------------------------------------------------------------------
// App state (zustand). Holds the simulated guild + the marketplace, recomputes
// scores, and exposes the mint and hire/settle actions.
// ---------------------------------------------------------------------------
import { create } from "zustand";
import type { GuildState, ReputationScore, CollusionFlag, Agent } from "./types";
import { runSimulation, DEFAULT_SIM, type SimConfig } from "./simulation";
import { scoreAgents, DEFAULT_PARAMS } from "./reputation";
import type { AggregatedGraph } from "./graph";
import { evaluateMint, mintBadge, DEFAULT_THRESHOLDS } from "./badges";
import { verifyCredential } from "./vc";
import { RNG } from "./random";
import {
  initMarket,
  postJob as mpPostJob,
  collectBids,
  award,
  settle,
  runTransactions,
  type MarketState,
} from "./marketplace";

interface Derived {
  scores: Map<string, ReputationScore>;
  flags: Map<string, CollusionFlag>;
  graph: AggregatedGraph;
}

interface GuildStore {
  guild: GuildState;
  derived: Derived;
  market: MarketState;
  marketTaskSeq: number;
  selectedId: string | null;
  config: SimConfig;
  lastMint: { agentId: string; ok: boolean; message: string } | null;
  activeJobId: string | null;

  initialize: (config?: SimConfig) => void;
  select: (id: string | null) => void;
  mint: (agentId: string) => void;
  tamperRandomAttestation: () => void;
  recompute: () => void;

  // Marketplace
  postJob: (requesterId: string, domain: string, budget: number) => string;
  awardBid: (jobId: string, bidId: string) => void;
  settleJob: (jobId: string) => void;
  simulateTransactions: (n: number) => void;
  setActiveJob: (jobId: string | null) => void;
}

function derive(guild: GuildState): Derived {
  const { scores, flags, graph } = scoreAgents(
    guild.agents,
    guild.attestations,
    guild.seedAgentIds,
    DEFAULT_PARAMS,
  );
  return { scores, flags, graph };
}

const trustGetter = (derived: Derived) => (id: string) =>
  derived.scores.get(id)?.trust ?? 0;

export const useGuild = create<GuildStore>((set, get) => ({
  guild: { agents: [], tasks: [], attestations: [], badges: [], step: 0, seedAgentIds: [] },
  derived: {
    scores: new Map(),
    flags: new Map(),
    graph: { agentIds: [], index: new Map(), edges: new Map(), out: new Map(), in: new Map() },
  },
  market: initMarket([]),
  marketTaskSeq: 0,
  selectedId: null,
  config: DEFAULT_SIM,
  lastMint: null,
  activeJobId: null,

  initialize: (config = DEFAULT_SIM) => {
    const guild = runSimulation(config);
    set({
      guild,
      derived: derive(guild),
      market: initMarket(guild.agents),
      marketTaskSeq: 0,
      config,
      selectedId: null,
      lastMint: null,
      activeJobId: null,
    });
  },

  select: (id) => set({ selectedId: id }),

  recompute: () => set({ derived: derive(get().guild) }),

  mint: (agentId) => {
    const { guild, derived } = get();
    const agent = guild.agents.find((a) => a.id === agentId);
    if (!agent) return;
    const score = derived.scores.get(agentId)!;
    const flag = derived.flags.get(agentId);
    const evaln = evaluateMint(agent, score, flag, guild.tasks, guild.attestations, DEFAULT_THRESHOLDS);
    if (!evaln.eligible || !evaln.tier) {
      set({ lastMint: { agentId, ok: false, message: evaln.reasons.join(" ") } });
      return;
    }
    const domainCounts = new Map<string, number>();
    for (const t of guild.tasks.filter((t) => t.agentId === agentId)) {
      domainCounts.set(t.domain, (domainCounts.get(t.domain) ?? 0) + 1);
    }
    const topDomain =
      [...domainCounts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? "general";
    const badge = mintBadge(agent, evaln.tier, topDomain, evaln, guild.step);
    const badges = [...guild.badges.filter((b) => !(b.subjectId === agentId)), badge];
    set({
      guild: { ...guild, badges },
      lastMint: {
        agentId,
        ok: true,
        message: `Minted ${evaln.tier.toUpperCase()} badge. Credential signature verified: ${badge.verified}.`,
      },
    });
  },

  tamperRandomAttestation: () => {
    const { guild } = get();
    if (guild.attestations.length === 0) return;
    const idx = Math.floor(Math.random() * guild.attestations.length);
    const target = guild.attestations[idx];
    const tampered = {
      ...target,
      credential: {
        ...target.credential,
        credentialSubject: { ...target.credential.credentialSubject, rating: 1 },
      },
    };
    tampered.verified = verifyCredential(tampered.credential);
    tampered.rating = 1;
    const attestations = guild.attestations.slice();
    attestations[idx] = tampered;
    const newGuild = { ...guild, attestations };
    set({ guild: newGuild, derived: derive(newGuild) });
  },

  // --- Marketplace ----------------------------------------------------------

  postJob: (requesterId, domain, budget) => {
    const { market, guild, derived } = get();
    const title = `${domain} contract`;
    const posted = mpPostJob(market, requesterId, domain, title, budget, guild.step);
    const withBids = collectBids(posted.market, posted.jobId, guild.agents, trustGetter(derived), new RNG((Date.now() ^ posted.market.seq) >>> 0));
    set({ market: withBids, activeJobId: posted.jobId });
    return posted.jobId;
  },

  awardBid: (jobId, bidId) => {
    const { market, guild } = get();
    set({ market: award(market, jobId, bidId, guild.step) });
  },

  settleJob: (jobId) => {
    const { market, guild, marketTaskSeq } = get();
    const agentMap = new Map<string, Agent>(guild.agents.map((a) => [a.id, a]));
    const rng = new RNG((Date.now() ^ (market.seq + 1)) >>> 0);
    const { market: m2, result } = settle(market, jobId, agentMap, guild.step, marketTaskSeq, rng);
    if (!result) {
      set({ market: m2 });
      return;
    }
    const newGuild: GuildState = {
      ...guild,
      tasks: [...guild.tasks, result.task],
      attestations: [...guild.attestations, result.attestation],
    };
    set({
      market: m2,
      guild: newGuild,
      marketTaskSeq: marketTaskSeq + 1,
      derived: derive(newGuild), // settlement feeds the reputation graph
    });
  },

  simulateTransactions: (n) => {
    const { market, guild, derived, marketTaskSeq } = get();
    const { market: m2, tasks, attestations } = runTransactions(
      market,
      guild.agents,
      trustGetter(derived),
      n,
      guild.step,
      marketTaskSeq,
      (Date.now() ^ market.seq) >>> 0,
    );
    const newGuild: GuildState = {
      ...guild,
      tasks: [...guild.tasks, ...tasks],
      attestations: [...guild.attestations, ...attestations],
    };
    set({
      market: m2,
      guild: newGuild,
      marketTaskSeq: marketTaskSeq + tasks.length,
      derived: derive(newGuild),
    });
  },

  setActiveJob: (jobId) => set({ activeJobId: jobId }),
}));
