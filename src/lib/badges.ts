// ---------------------------------------------------------------------------
// Soulbound credential minting.
// A badge is a non-transferable VC issued by the Guild authority DID to an
// agent's DID once it crosses defined thresholds. There is no owner/transfer
// field anywhere in the type — non-transferability is structural, not a flag.
// ---------------------------------------------------------------------------
import { generateKeyPair, didFromPublicKey } from "./crypto";
import { issueCredential, verifyCredential } from "./vc";
import type {
  Agent,
  Badge,
  BadgeTier,
  ReputationScore,
  CollusionFlag,
  Task,
  Attestation,
} from "./types";

// The Guild authority is a well-known issuer with its own keypair. In a real
// deployment this would be a governance contract / DAO key. Deterministic here.
const GUILD_SEED = new Uint8Array(32).fill(7);
export const GUILD_KEYS = generateKeyPair(GUILD_SEED);
export const GUILD_DID = didFromPublicKey(GUILD_KEYS.publicKeyHex);

export interface Thresholds {
  bronze: { trust: number; tasks: number; reviewers: number };
  silver: { trust: number; tasks: number; reviewers: number };
  gold: { trust: number; tasks: number; reviewers: number };
  /** Max collusion suspicion allowed to mint anything. */
  maxSuspicion: number;
}

export const DEFAULT_THRESHOLDS: Thresholds = {
  bronze: { trust: 45, tasks: 3, reviewers: 2 },
  silver: { trust: 65, tasks: 6, reviewers: 4 },
  gold: { trust: 80, tasks: 10, reviewers: 6 },
  maxSuspicion: 0.5,
};

export interface MintEvaluation {
  eligible: boolean;
  tier: BadgeTier | null;
  reasons: string[];
  metrics: {
    trustScore: number;
    tasksCompleted: number;
    attestationsReceived: number;
    distinctReviewers: number;
    suspicion: number;
  };
}

/** Decide whether an agent qualifies for a badge and at what tier. */
export function evaluateMint(
  agent: Agent,
  score: ReputationScore,
  flag: CollusionFlag | undefined,
  tasks: Task[],
  attestations: Attestation[],
  thresholds: Thresholds = DEFAULT_THRESHOLDS,
): MintEvaluation {
  const tasksCompleted = tasks.filter((t) => t.agentId === agent.id).length;
  const received = attestations.filter((a) => a.subjectId === agent.id && a.verified);
  const distinctReviewers = new Set(received.map((a) => a.reviewerId)).size;
  const suspicion = flag?.suspicion ?? 0;
  const metrics = {
    trustScore: score.trust,
    tasksCompleted,
    attestationsReceived: received.length,
    distinctReviewers,
    suspicion,
  };

  const reasons: string[] = [];

  if (suspicion > thresholds.maxSuspicion) {
    reasons.push(
      `Blocked: collusion suspicion ${(suspicion * 100).toFixed(0)}% exceeds limit ${(thresholds.maxSuspicion * 100).toFixed(0)}%.`,
    );
    return { eligible: false, tier: null, reasons, metrics };
  }

  const meets = (t: { trust: number; tasks: number; reviewers: number }) =>
    score.trust >= t.trust &&
    tasksCompleted >= t.tasks &&
    distinctReviewers >= t.reviewers;

  let tier: BadgeTier | null = null;
  if (meets(thresholds.gold)) tier = "gold";
  else if (meets(thresholds.silver)) tier = "silver";
  else if (meets(thresholds.bronze)) tier = "bronze";

  if (!tier) {
    reasons.push(
      `Not yet eligible. Need trust ≥ ${thresholds.bronze.trust} (have ${score.trust.toFixed(0)}), ` +
        `tasks ≥ ${thresholds.bronze.tasks} (have ${tasksCompleted}), ` +
        `distinct reviewers ≥ ${thresholds.bronze.reviewers} (have ${distinctReviewers}).`,
    );
    return { eligible: false, tier: null, reasons, metrics };
  }

  reasons.push(
    `Qualifies for ${tier.toUpperCase()}: trust ${score.trust.toFixed(0)}, ` +
      `${tasksCompleted} tasks, ${distinctReviewers} distinct reviewers, ` +
      `suspicion ${(suspicion * 100).toFixed(0)}%.`,
  );
  return { eligible: true, tier, reasons, metrics };
}

/** Mint a soulbound badge VC for an agent (assumes eligibility already checked). */
export function mintBadge(
  agent: Agent,
  tier: BadgeTier,
  domain: string | "general",
  evaluation: MintEvaluation,
  step: number,
): Badge {
  const id = `urn:badge:${agent.id}:${tier}:${step}`;
  const label = `Accredited Agent — ${domain === "general" ? "General" : domain} (${tier})`;
  const credential = issueCredential({
    id,
    type: ["GuildAccreditation", "SoulboundCredential"],
    issuerDid: GUILD_DID,
    issuerPrivateKeyHex: GUILD_KEYS.privateKeyHex,
    subjectDid: agent.did,
    taskId: "n/a",
    rating: evaluation.metrics.trustScore / 100,
    domain: String(domain),
  });
  return {
    id,
    subjectId: agent.id,
    tier,
    label,
    domain,
    issuedAtStep: step,
    evidence: {
      trustScore: evaluation.metrics.trustScore,
      tasksCompleted: evaluation.metrics.tasksCompleted,
      attestationsReceived: evaluation.metrics.attestationsReceived,
      distinctReviewers: evaluation.metrics.distinctReviewers,
    },
    credential,
    verified: verifyCredential(credential),
  };
}
