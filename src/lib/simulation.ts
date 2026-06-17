// ---------------------------------------------------------------------------
// Simulation: spins up a population of agents (good / new / incompetent /
// colluding / Sybil) and generates tasks + cryptographically signed
// attestations across several rounds. Fully deterministic for a given seed.
// ---------------------------------------------------------------------------
import type { Agent, Archetype, Attestation, Task, GuildState } from "./types";
import { generateKeyPair, didFromPublicKey } from "./crypto";
import { issueCredential, verifyCredential } from "./vc";
import { RNG, clamp01 } from "./random";

const DOMAINS = ["data-extraction", "summarization", "code-review", "translation", "research"];

const HANDLE_ADJ = [
  "Swift", "Quiet", "Iron", "Lucid", "Amber", "Cobalt", "Vivid", "Stark",
  "Nimble", "Solar", "Onyx", "Pale", "Brisk", "Calm", "Keen", "Bright",
  "Tidal", "Ember", "Frost", "Vector", "Helix", "Quartz", "Cipher", "Echo",
];
const HANDLE_NOUN = [
  "Parser", "Scribe", "Forge", "Pilot", "Oracle", "Mason", "Falcon", "Atlas",
  "Loom", "Probe", "Beacon", "Cartographer", "Sentinel", "Drafter", "Analyst",
  "Courier", "Synth", "Ranger", "Weaver", "Compiler", "Auditor", "Sage", "Relay",
];

export interface SimConfig {
  seed: number;
  steps: number;
  counts: Record<Exclude<Archetype, never>, number>;
  /** Number of colluder rings to split the colluder population into. */
  colluderRings: number;
}

export const DEFAULT_SIM: SimConfig = {
  seed: 42,
  steps: 8,
  counts: { honest: 14, newcomer: 4, incompetent: 6, colluder: 6, sybil: 3 },
  colluderRings: 2,
};

function competenceFor(arch: Archetype, rng: RNG): number {
  switch (arch) {
    case "honest":
      return rng.range(0.72, 0.96);
    case "newcomer":
      return rng.range(0.65, 0.9);
    case "incompetent":
      return rng.range(0.1, 0.4);
    case "colluder":
      return rng.range(0.25, 0.55);
    case "sybil":
      return rng.range(0.05, 0.3);
  }
}

/** How a reviewer rates a subject's work, by reviewer archetype. */
function rate(
  reviewer: Agent,
  subject: Agent,
  quality: number,
  sameRing: boolean,
  rng: RNG,
): number {
  switch (reviewer.archetype) {
    case "honest":
    case "newcomer":
      return clamp01(quality + rng.gauss(0, 0.08)); // fair, low noise
    case "incompetent":
      return clamp01(0.5 + rng.gauss(0, 0.25)); // noisy, near-random
    case "colluder":
      if (sameRing) return clamp01(rng.range(0.9, 1.0)); // rubber-stamp ring-mates
      if (subject.archetype === "honest") return clamp01(quality * 0.5 + rng.gauss(0, 0.1)); // smear rivals
      return clamp01(quality + rng.gauss(0, 0.12));
    case "sybil":
      if (sameRing) return clamp01(rng.range(0.92, 1.0)); // farm boosts the farm
      return clamp01(quality + rng.gauss(0, 0.15));
  }
}

export function runSimulation(config: SimConfig = DEFAULT_SIM): GuildState {
  const rng = new RNG(config.seed);
  const agents: Agent[] = [];
  const usedHandles = new Set<string>();

  const makeHandle = (): string => {
    for (let i = 0; i < 200; i++) {
      const h = `${rng.pick(HANDLE_ADJ)}${rng.pick(HANDLE_NOUN)}`;
      if (!usedHandles.has(h)) {
        usedHandles.add(h);
        return h;
      }
    }
    return `Agent${usedHandles.size}`;
  };

  const addAgents = (arch: Archetype, count: number, ringAssign?: (i: number) => string) => {
    for (let i = 0; i < count; i++) {
      const keys = generateKeyPair(rng.bytes32());
      const did = didFromPublicKey(keys.publicKeyHex);
      const id = `agent-${agents.length}`;
      // Newcomers join late; everyone else exists from the start.
      const createdAtStep = arch === "newcomer" ? config.steps - 1 - rng.int(2) : 0;
      const domains = [rng.pick(DOMAINS)];
      if (rng.next() > 0.5) domains.push(rng.pick(DOMAINS));
      agents.push({
        id,
        did,
        handle: makeHandle(),
        archetype: arch,
        keys,
        createdAtStep: Math.max(0, createdAtStep),
        domains: [...new Set(domains)],
        trueCompetence: competenceFor(arch, rng),
        ringId: ringAssign ? ringAssign(i) : undefined,
      });
    }
  };

  addAgents("honest", config.counts.honest);
  addAgents("newcomer", config.counts.newcomer);
  addAgents("incompetent", config.counts.incompetent);
  addAgents("colluder", config.counts.colluder, (i) => `ring-${i % config.colluderRings}`);
  addAgents("sybil", config.counts.sybil, () => "sybil-farm");

  // Pre-trusted seeds: the 3 most competent honest agents. This is the
  // Sybil-resistance anchor — trust must ultimately originate here.
  const seedAgentIds = agents
    .filter((a) => a.archetype === "honest")
    .sort((a, b) => b.trueCompetence - a.trueCompetence)
    .slice(0, 3)
    .map((a) => a.id);

  const byId = new Map(agents.map((a) => [a.id, a]));
  const ringMembers = new Map<string, Agent[]>();
  for (const a of agents) {
    if (!a.ringId) continue;
    if (!ringMembers.has(a.ringId)) ringMembers.set(a.ringId, []);
    ringMembers.get(a.ringId)!.push(a);
  }

  const tasks: Task[] = [];
  const attestations: Attestation[] = [];

  const tasksPerStep = (a: Agent): number => {
    switch (a.archetype) {
      case "honest":
        return 2;
      case "newcomer":
        return 1;
      case "incompetent":
        return 2;
      case "colluder":
        return 2;
      case "sybil":
        return 1;
    }
  };

  for (let step = 0; step < config.steps; step++) {
    const active = agents.filter((a) => a.createdAtStep <= step);

    for (const performer of active) {
      const nTasks = tasksPerStep(performer);
      for (let k = 0; k < nTasks; k++) {
        const domain = rng.pick(performer.domains);
        const quality = clamp01(performer.trueCompetence + rng.gauss(0, 0.1));
        const task: Task = {
          id: `task-${tasks.length}`,
          agentId: performer.id,
          domain,
          title: `${domain} job #${tasks.length}`,
          step,
          qualityTrue: quality,
        };
        tasks.push(task);

        // Assemble reviewers.
        const reviewers = new Map<string, Agent>();
        // Ring-mates always review each other (builds reciprocal high edges).
        if (performer.ringId) {
          for (const m of ringMembers.get(performer.ringId)!) {
            if (m.id !== performer.id) reviewers.set(m.id, m);
          }
        }
        // A few external reviewers, biased toward honest/established agents.
        const pool = active.filter((a) => a.id !== performer.id);
        const externalCount = performer.archetype === "sybil" ? 1 : 2 + rng.int(2);
        for (let r = 0; r < externalCount && pool.length > 0; r++) {
          // Weighted pick: honest agents are likelier to be assigned as reviewers.
          const candidate = weightedReviewerPick(pool, rng);
          reviewers.set(candidate.id, candidate);
        }

        for (const reviewer of reviewers.values()) {
          const sameRing =
            !!performer.ringId && performer.ringId === reviewer.ringId;
          const rating = rate(reviewer, performer, quality, sameRing, rng);
          const credential = issueCredential({
            id: `urn:att:${step}:${reviewer.id}:${task.id}`,
            type: ["WorkAttestation"],
            issuerDid: reviewer.did,
            issuerPrivateKeyHex: reviewer.keys.privateKeyHex,
            subjectDid: performer.did,
            taskId: task.id,
            rating: Math.round(rating * 1000) / 1000,
            domain,
          });
          attestations.push({
            id: credential.id,
            taskId: task.id,
            reviewerId: reviewer.id,
            subjectId: performer.id,
            rating: Math.round(rating * 1000) / 1000,
            step,
            credential,
            verified: verifyCredential(credential),
          });
        }
      }
    }
  }

  void byId; // (kept for clarity / potential extension)

  return { agents, tasks, attestations, badges: [], step: config.steps, seedAgentIds };
}

function weightedReviewerPick(pool: Agent[], rng: RNG): Agent {
  const weight = (a: Agent) =>
    a.archetype === "honest" ? 3 : a.archetype === "newcomer" ? 1.5 : 1;
  const total = pool.reduce((s, a) => s + weight(a), 0);
  let r = rng.next() * total;
  for (const a of pool) {
    r -= weight(a);
    if (r <= 0) return a;
  }
  return pool[pool.length - 1];
}
