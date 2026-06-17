// ---------------------------------------------------------------------------
// Marketplace / transaction layer.
//
// Agent A hires Agent B. Agent Guild provides discovery (reputation-ranked
// bids), escrow, settlement, and — crucially — generates a signed attestation
// from the completed job that feeds straight back into the reputation graph.
// The Guild takes a 0.1% fee on the transaction value.
//
// Everything is local and simulated: no real money, no chain. "Credits" are a
// unit of account only.
// ---------------------------------------------------------------------------
import type { Agent, Attestation, Task } from "./types";
import { issueCredential, verifyCredential } from "./vc";
import { RNG, clamp01 } from "./random";

export const FEE_BPS = 10; // 10 basis points = 0.1%
export const START_BALANCE = 5000;

export type JobStatus = "open" | "awarded" | "in_progress" | "settled" | "cancelled";

export interface Bid {
  id: string;
  jobId: string;
  bidderId: string;
  price: number;
  /** The bidder's trust score at bid time — the discovery signal. */
  trustAtBid: number;
  /** trust per credit — how bids are ranked. */
  value: number;
}

export interface Job {
  id: string;
  requesterId: string;
  domain: string;
  title: string;
  budget: number;
  createdAtStep: number;
  status: JobStatus;
  bids: Bid[];
  performerId?: string;
  price?: number;
  resultQuality?: number;
  settledAtStep?: number;
  attestationId?: string;
}

export interface Escrow {
  id: string;
  jobId: string;
  amount: number;
  status: "held" | "released" | "refunded";
}

export type LedgerType = "escrow_lock" | "payment_release" | "fee" | "refund";

export interface LedgerEntry {
  id: string;
  step: number;
  type: LedgerType;
  jobId: string;
  from?: string;
  to?: string;
  amount: number;
  label: string;
}

export interface MarketState {
  wallets: Record<string, number>;
  treasury: number;
  feeBps: number;
  jobs: Job[];
  escrows: Escrow[];
  ledger: LedgerEntry[];
  totals: { volume: number; fees: number; count: number };
  seq: number;
}

export function feeOf(amount: number, feeBps = FEE_BPS): number {
  return Math.round(amount * feeBps) / 10000; // amount * bps/10000, 4dp
}

export function initMarket(agents: Agent[]): MarketState {
  const wallets: Record<string, number> = {};
  for (const a of agents) wallets[a.id] = START_BALANCE;
  return {
    wallets,
    treasury: 0,
    feeBps: FEE_BPS,
    jobs: [],
    escrows: [],
    ledger: [],
    totals: { volume: 0, fees: 0, count: 0 },
    seq: 0,
  };
}

const clone = (m: MarketState): MarketState => structuredClone(m);

// --- internal mutating helpers ---------------------------------------------

function _postJob(
  m: MarketState,
  requesterId: string,
  domain: string,
  title: string,
  budget: number,
  step: number,
): Job {
  const job: Job = {
    id: `job-${m.seq++}`,
    requesterId,
    domain,
    title,
    budget,
    createdAtStep: step,
    status: "open",
    bids: [],
  };
  m.jobs.push(job);
  return job;
}

/** Eligible agents bid; price competes, ranked by trust-per-credit. */
function _collectBids(
  m: MarketState,
  job: Job,
  agents: Agent[],
  trustOf: (id: string) => number,
  rng: RNG,
): void {
  const eligible = agents.filter(
    (a) => a.id !== job.requesterId && a.domains.includes(job.domain),
  );
  const bids: Bid[] = [];
  for (const a of eligible) {
    // More competent agents quote a little higher; all bid at/under budget.
    const factor = 0.55 + 0.4 * a.trueCompetence * rng.range(0.7, 1.1);
    const price = Math.max(5, Math.round(job.budget * clamp01(factor)));
    const trust = trustOf(a.id);
    bids.push({
      id: `bid-${m.seq++}`,
      jobId: job.id,
      bidderId: a.id,
      price,
      trustAtBid: trust,
      value: trust / Math.max(price, 1),
    });
  }
  bids.sort((x, y) => y.value - x.value); // best value first
  job.bids = bids;
}

function _award(m: MarketState, job: Job, bid: Bid, step: number): boolean {
  if ((m.wallets[job.requesterId] ?? 0) < bid.price) {
    job.status = "cancelled";
    return false;
  }
  m.wallets[job.requesterId] -= bid.price; // funds locked into escrow
  m.escrows.push({ id: `esc-${m.seq++}`, jobId: job.id, amount: bid.price, status: "held" });
  job.performerId = bid.bidderId;
  job.price = bid.price;
  job.status = "in_progress";
  m.ledger.push({
    id: `tx-${m.seq++}`,
    step,
    type: "escrow_lock",
    jobId: job.id,
    from: job.requesterId,
    amount: bid.price,
    label: `Escrow locked for ${job.title}`,
  });
  return true;
}

function _execute(job: Job, performer: Agent, rng: RNG): void {
  job.resultQuality = clamp01(performer.trueCompetence + rng.gauss(0, 0.1));
}

interface SettleResult {
  task: Task;
  attestation: Attestation;
}

/** Release escrow (minus fee) and mint a signed attestation from the result. */
function _settle(
  m: MarketState,
  job: Job,
  requester: Agent,
  performer: Agent,
  step: number,
  taskSeq: number,
  rng: RNG,
): SettleResult {
  const price = job.price!;
  const fee = feeOf(price, m.feeBps);
  const payout = Math.round((price - fee) * 10000) / 10000;

  m.wallets[performer.id] = (m.wallets[performer.id] ?? 0) + payout;
  m.treasury += fee;
  const esc = m.escrows.find((e) => e.jobId === job.id);
  if (esc) esc.status = "released";

  m.ledger.push({
    id: `tx-${m.seq++}`,
    step,
    type: "payment_release",
    jobId: job.id,
    from: requester.id,
    to: performer.id,
    amount: payout,
    label: `Paid ${performer.handle} for ${job.title}`,
  });
  m.ledger.push({
    id: `tx-${m.seq++}`,
    step,
    type: "fee",
    jobId: job.id,
    from: requester.id,
    to: "guild-treasury",
    amount: fee,
    label: `Guild fee (0.1%)`,
  });
  m.totals.volume += price;
  m.totals.fees += fee;
  m.totals.count += 1;

  // The requester honestly attests to the delivered quality. This signed VC
  // is the real product — it joins the reputation graph.
  const quality = job.resultQuality ?? performer.trueCompetence;
  const rating = clamp01(quality + rng.gauss(0, 0.05));
  const taskId = `mtask-${taskSeq}`;
  const credential = issueCredential({
    id: `urn:att:market:${job.id}`,
    type: ["WorkAttestation", "MarketSettlement"],
    issuerDid: requester.did,
    issuerPrivateKeyHex: requester.keys.privateKeyHex,
    subjectDid: performer.did,
    taskId,
    rating: Math.round(rating * 1000) / 1000,
    domain: job.domain,
  });
  const attestation: Attestation = {
    id: credential.id,
    taskId,
    reviewerId: requester.id,
    subjectId: performer.id,
    rating: Math.round(rating * 1000) / 1000,
    step,
    credential,
    verified: verifyCredential(credential),
  };
  const task: Task = {
    id: taskId,
    agentId: performer.id,
    domain: job.domain,
    title: `${job.title} (hired)`,
    step,
    qualityTrue: quality,
  };

  job.status = "settled";
  job.settledAtStep = step;
  job.attestationId = attestation.id;
  return { task, attestation };
}

// --- public, granular actions (each returns fresh state) -------------------

export function postJob(
  m: MarketState,
  requesterId: string,
  domain: string,
  title: string,
  budget: number,
  step: number,
): { market: MarketState; jobId: string } {
  const next = clone(m);
  const job = _postJob(next, requesterId, domain, title, budget, step);
  return { market: next, jobId: job.id };
}

export function collectBids(
  m: MarketState,
  jobId: string,
  agents: Agent[],
  trustOf: (id: string) => number,
  rng: RNG,
): MarketState {
  const next = clone(m);
  const job = next.jobs.find((j) => j.id === jobId);
  if (job && job.status === "open") _collectBids(next, job, agents, trustOf, rng);
  return next;
}

export function award(m: MarketState, jobId: string, bidId: string, step: number): MarketState {
  const next = clone(m);
  const job = next.jobs.find((j) => j.id === jobId);
  const bid = job?.bids.find((b) => b.id === bidId);
  if (job && bid && job.status === "open") _award(next, job, bid, step);
  return next;
}

/** Execute then settle in one move, returning the new state + the records to
 *  append to the guild (task + signed attestation). */
export function settle(
  m: MarketState,
  jobId: string,
  agents: Map<string, Agent>,
  step: number,
  taskSeq: number,
  rng: RNG,
): { market: MarketState; result?: SettleResult } {
  const next = clone(m);
  const job = next.jobs.find((j) => j.id === jobId);
  if (!job || job.status !== "in_progress" || !job.performerId) return { market: next };
  const requester = agents.get(job.requesterId)!;
  const performer = agents.get(job.performerId)!;
  _execute(job, performer, rng);
  const result = _settle(next, job, requester, performer, step, taskSeq, rng);
  return { market: next, result };
}

// --- bulk simulation --------------------------------------------------------

export interface BulkResult {
  market: MarketState;
  tasks: Task[];
  attestations: Attestation[];
}

/** Run `n` complete hire→escrow→settle transactions between random agents.
 *  Drives transaction volume so the revenue dashboard shows fees scaling. */
export function runTransactions(
  m: MarketState,
  agents: Agent[],
  trustOf: (id: string) => number,
  n: number,
  step: number,
  taskSeqStart: number,
  seed: number,
): BulkResult {
  const next = clone(m);
  const rng = new RNG(seed);
  const agentMap = new Map(agents.map((a) => [a.id, a]));
  const tasks: Task[] = [];
  const attestations: Attestation[] = [];
  const domains = [...new Set(agents.flatMap((a) => a.domains))];
  let taskSeq = taskSeqStart;

  for (let i = 0; i < n; i++) {
    const requester = rng.pick(agents);
    const domain = rng.pick(domains);
    const budget = 50 + rng.int(450);
    const job = _postJob(next, requester.id, domain, `${domain} contract`, budget, step);
    _collectBids(next, job, agents, trustOf, rng);
    if (job.bids.length === 0) {
      job.status = "cancelled";
      continue;
    }
    // Requester picks the best-value bid (reputation-driven discovery).
    const bid = job.bids[0];
    if (!_award(next, job, bid, step)) continue;
    const performer = agentMap.get(job.performerId!)!;
    _execute(job, performer, rng);
    const r = _settle(next, job, requester, performer, step, taskSeq++, rng);
    tasks.push(r.task);
    attestations.push(r.attestation);
  }
  return { market: next, tasks, attestations };
}
