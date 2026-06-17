// ---------------------------------------------------------------------------
// Agent Guild — core domain types
// ---------------------------------------------------------------------------
// The reputation layer is the product. Everything the scoring engine sees lives
// in Agents, Tasks and Attestations. The cryptographic credential (badge) is
// only a portable, signed container for what an agent has earned.
// ---------------------------------------------------------------------------

export type Archetype =
  | "honest" // competent, reviews others fairly
  | "newcomer" // competent but few tasks/attestations (cold start)
  | "incompetent" // produces low-quality work, reviews are noisy
  | "colluder" // ring member: inflates ring-mates, may smear outsiders
  | "sybil"; // freshly spun-up identities used to farm trust

export interface KeyPair {
  /** ed25519 public key, raw 32 bytes, hex-encoded. */
  publicKeyHex: string;
  /** ed25519 private/seed key, raw 32 bytes, hex-encoded. (Local demo only.) */
  privateKeyHex: string;
}

export interface Agent {
  id: string;
  /** Decentralized identifier — did:key derived from the ed25519 public key. */
  did: string;
  handle: string;
  archetype: Archetype;
  keys: KeyPair;
  /** Simulation step at which the identity was created (for cold-start logic). */
  createdAtStep: number;
  /** Skill domains the agent works in. */
  domains: string[];

  // --- Ground truth: known ONLY to the simulator, never to the scorer. -------
  /** True competence in [0,1]. Used to generate task outcomes & to grade the
   *  reputation system afterwards. The scoring engine must never read this. */
  trueCompetence: number;
  /** Members of this agent's collusion ring (agent ids), if any. */
  ringId?: string;
}

export interface Task {
  id: string;
  agentId: string; // performer
  domain: string;
  title: string;
  step: number;
  /** Objective quality of the delivered work in [0,1] (sim ground truth). */
  qualityTrue: number;
}

/** A W3C-VC-shaped, ed25519-signed attestation. The `rating` is the machine-
 *  readable claim; `proof` makes it verifiable and non-repudiable. */
export interface Attestation {
  id: string;
  taskId: string;
  reviewerId: string; // issuer
  subjectId: string; // who is being reviewed
  /** Normalised quality rating in [0,1]. */
  rating: number;
  step: number;
  /** The full Verifiable Credential JSON (with proof). */
  credential: VerifiableCredential;
  /** Result of cryptographically verifying `credential` at load time. */
  verified: boolean;
}

// --- W3C Verifiable Credential (Data Model 2.0, simplified) -----------------

export interface VerifiableCredential {
  "@context": string[];
  id: string;
  type: string[];
  issuer: string; // reviewer DID
  validFrom: string; // ISO timestamp
  credentialSubject: {
    id: string; // subject DID
    taskId: string;
    rating: number;
    domain: string;
  };
  proof: Proof;
}

export interface Proof {
  type: "Ed25519Signature2020";
  created: string;
  verificationMethod: string; // issuer DID#key
  proofPurpose: "assertionMethod";
  /** Hex-encoded ed25519 signature over the canonicalised credential. */
  proofValue: string;
}

// --- Soulbound credential (the "badge" / machine CV entry) ------------------

export type BadgeTier = "bronze" | "silver" | "gold";

export interface Badge {
  id: string;
  subjectId: string;
  tier: BadgeTier;
  /** Human label, e.g. "Accredited Agent — Data Extraction". */
  label: string;
  domain: string | "general";
  issuedAtStep: number;
  /** Snapshot of the metrics that justified the mint. */
  evidence: {
    trustScore: number;
    tasksCompleted: number;
    attestationsReceived: number;
    distinctReviewers: number;
  };
  /** Soulbound VC: issued by the Guild authority DID to the agent's DID,
   *  non-transferable by construction (no `owner`/transfer field exists). */
  credential: VerifiableCredential;
  verified: boolean;
}

// --- Scoring outputs --------------------------------------------------------

export interface ReputationScore {
  agentId: string;
  /** Final trust score in [0,100]. */
  trust: number;
  /** Raw recursive (EigenTrust) global trust, pre-penalty, in [0,1]. */
  eigenTrust: number;
  /** Quality signal from attestations received, reviewer-weighted, [0,1]. */
  weightedQuality: number;
  /** How well this agent's own reviews matched consensus, [0,1]. */
  endorsementAccuracy: number;
  /** Penalty applied for suspected collusion, [0,1]. */
  collusionPenalty: number;
  /** Confidence in the score given volume/diversity of evidence, [0,1]. */
  confidence: number;
  rank: number;
}

export interface CollusionFlag {
  agentId: string;
  /** [0,1] — higher means more collusion-like behaviour. */
  suspicion: number;
  reasons: string[];
  /** Detected community id this agent belongs to (if clustered). */
  clusterId?: number;
}

export interface GuildState {
  agents: Agent[];
  tasks: Task[];
  attestations: Attestation[];
  badges: Badge[];
  step: number;
  /** Pre-trusted seed agent ids — the Sybil-resistance anchor. */
  seedAgentIds: string[];
}
