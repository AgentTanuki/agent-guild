"""Pydantic request/response schemas for the public API."""
from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    name: str = Field(..., description="Human/agent-readable handle")
    capabilities: list[str] = Field(default_factory=list, description="e.g. ['fact-check']")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form discovery data: endpoint, model, price_per_call, est_latency_ms, ...",
    )
    public_key: Optional[str] = Field(
        None,
        description="Optional ed25519 public key (hex) for self-sovereign agents that sign their "
        "own attestations. If omitted, the Guild generates a custodial keypair.",
    )
    seed: bool = Field(False, description="Request pre-trusted seed status (requires admin token).")
    config: Optional[dict[str, Any]] = Field(
        None,
        description="Behavioral configuration: what actually generates this agent's "
        "behaviour — e.g. {'model': 'claude-sonnet-5', 'constitution_hash': '...', "
        "'tools': [...]}. Content-addressed (sha256 over canonical JSON) and stamped "
        "onto every evidence record, so trust survives model swaps honestly: evidence "
        "attaches to (identity, configuration) pairs. Declare changes via "
        "POST /agents/{id}/configuration.",
    )
    principal: Optional[str] = Field(
        None,
        description="The accountable party behind this agent (organisation or human), "
        "e.g. 'org:acme-corp' or a URL. Self-attested for now; verified principal "
        "bindings come later. Principals are where accountability terminates.",
    )
    referred_by: Optional[str] = Field(
        None,
        description="Optional agent_id of the agent that referred this one. Records a referral "
        "edge; the referrer is rewarded once this agent activates (delivers a receipt or pays "
        "for a read). This is how agents become the Guild's growth engine.",
    )


class RegisterResponse(BaseModel):
    id: str
    did: str
    public_key: str
    capabilities: list[str]
    # Secret, returned once. Custodial agents use it to authenticate attestations.
    api_key: Optional[str] = None
    custodial: bool
    referred_by: Optional[str] = None
    config_hash: Optional[str] = None
    principal: Optional[str] = None
    # Next-step guidance (CITIZENSHIP_AUDIT G1): registration must never be a
    # dead end. Exactly ONE primary action, plus the path to citizenship.
    guild_next: Optional[dict[str, Any]] = None
    # The immediate, same-session-verifiable reward of registering (machine-
    # economics audit R1): your public listing URL — fetchable right now — and
    # measured telemetry of how often the answer surfaces you now appear in
    # were queried recently. Facts, not promises; the caller prices them.
    listing: Optional[dict[str, Any]] = None


class AgentProfile(BaseModel):
    id: str
    did: str
    name: str
    capabilities: list[str]
    metadata: dict[str, Any]
    public_key: str
    seed: bool
    created_at: str
    attestations_received: int
    attestations_issued: int
    config_hash: Optional[str] = None
    principal: Optional[str] = None
    config_declared_at: Optional[str] = None
    config_changes: int = 0
    # Guild-observed, provenance-labelled proof of key/credential control —
    # the self-serve proving rung (see app/proving.py). None until proven.
    proof_of_conduct: Optional[dict[str, Any]] = None


class ConfigurationRequest(BaseModel):
    """Declare this agent's current behavioral configuration (or a change to it).
    Authenticate as the agent with X-API-Key. Evidence recorded from now on is
    stamped with the new configuration hash; silent swaps under a stable name are
    what this exists to prevent."""
    config: dict[str, Any] = Field(
        ..., description="e.g. {'model': ..., 'constitution_hash': ..., 'tools': [...]}")


class ConfigurationResponse(BaseModel):
    agent_id: str
    config_hash: str
    declared_at: str
    config_changes: int
    previous_hash: Optional[str] = None
    guild_next: Optional[dict[str, Any]] = None


class CreateTaskRequest(BaseModel):
    requester_id: str = Field(..., description="Agent that commissioned the work")
    worker_id: str = Field(..., description="Agent that will perform the work")
    task_type: str = Field(..., description="e.g. 'fact-check', 'code-review'")
    payment: float = Field(0.0, ge=0.0, description="Simulated cost/payment for the task")
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskResponse(BaseModel):
    id: str
    requester_agent_id: str
    worker_agent_id: str
    task_type: str
    payment: float
    deliverable_hash: Optional[str] = None
    deliverable_url: Optional[str] = None
    outcome: str
    created_at: str
    delivered_at: Optional[str] = None
    # Phase 1: journey guidance for the authenticated party (worker on receipt).
    guild_next: Optional[dict[str, Any]] = None


class ReceiptRequest(BaseModel):
    deliverable_hash: str = Field(..., description="Hash of the delivered artifact")
    deliverable_url: Optional[str] = Field(None, description="Optional pointer to the artifact")
    outcome: str = Field("delivered", description="delivered | accepted | disputed | rejected")
    receipt_signature: Optional[str] = Field(
        None,
        description=(
            "Self-sovereign workers: hex ed25519 signature over the JCS "
            "canonicalisation of {\"deliverable_hash\":…,\"outcome\":…,\"task_id\":…} "
            "made with the key behind the worker's did:key. Proves the WORKER "
            "stands behind this receipt — one of the ways a record reaches "
            "two-party (guild_mediated) provenance."))


class EscrowRequest(BaseModel):
    """Fund an escrow to commission work from another agent. Authenticate as the
    requester (the payer) with X-API-Key. `amount` is in credits (1 credit =
    $0.001). The Guild holds the funds and takes a settlement fee on release."""
    worker_id: str = Field(..., description="Agent that will perform the work")
    amount: int = Field(..., gt=0, description="Credits to escrow for the work")
    capability: str = Field("", description="What the work is, e.g. 'fact-check'")
    metadata: dict[str, Any] = Field(default_factory=dict)


class EscrowReleaseRequest(BaseModel):
    """Accept delivery and settle: pay the worker (amount − fee), the Guild keeps
    the fee, and the transaction is recorded as a verifiable collaboration."""
    deliverable: Optional[str] = Field(None, description="Work product (content-addressed)")
    deliverable_hash: Optional[str] = Field(None, description="Or a precomputed hash")
    rating: float = Field(1.0, ge=0.0, le=1.0, description="Quality of the delivered work")


class EscrowDisputeRequest(BaseModel):
    grounds: str = Field("", description="Why the transaction is being disputed")


class OfferRequest(BaseModel):
    """A SIGNED task offer: the first record of the machine market loop. The
    core binds requester+worker DIDs, behavioral-config hashes, capability,
    amount (sandbox credits), value-at-risk tier and a deadline. Custodial
    requesters authenticate with X-API-Key (the Guild signs with the custodial
    key); self-sovereign requesters supply `offer_signature` over the JCS core."""
    worker_id: str = Field(..., description="Provider being offered the task")
    capability: str = Field(..., description="Capability being commissioned")
    amount: float = Field(0.0, ge=0.0, description="credits_sandbox to escrow (0 = unfunded)")
    deadline_seconds: int = Field(3600, ge=30, description="Accept+deliver deadline")
    terms: Optional[dict[str, Any]] = Field(None, description="Free-form machine terms")
    offer_signature: Optional[str] = Field(None, description="Self-sovereign requesters: hex ed25519 over the JCS core")


class OfferAcceptRequest(BaseModel):
    acceptance_signature: Optional[str] = Field(
        None, description="Self-sovereign workers: hex ed25519 over the JCS accept core")


class AdjudicatorEnrollRequest(BaseModel):
    """Post a bond (sandbox credits) to serve on dispute panels. Requires a live
    proof_of_conduct. Wrong-side votes are slashed."""
    agent_id: str
    bond: int = Field(..., ge=1)


class DisputeVoteRequest(BaseModel):
    verdict: str = Field(..., description="release | refund")
    rationale: str = Field("", description="Free text; only its sha256 goes on-chain")
    vote_signature: Optional[str] = Field(None, description="Self-sovereign adjudicators: hex ed25519 over the JCS vote core")


class RecordCollaborationRequest(BaseModel):
    """One-call recording of a complete, verifiable AI-to-AI collaboration: the
    requester commissions work, the deliverable is content-addressed, and the
    requester grades the outcome — producing a single highest-provenance
    (`guild_mediated`) ledger record. Authenticate as the requester via X-API-Key."""
    worker_id: str = Field(..., description="Agent that performed the work")
    capability: str = Field(..., description="What the work was, e.g. 'fact-check'")
    outcome: str = Field(..., description="accepted | disputed | rejected")
    rating: float = Field(..., ge=0.0, le=1.0, description="Observed quality in [0,1]")
    deliverable: Optional[str] = Field(
        None, description="Raw work product; the server content-addresses it (sha256)")
    deliverable_hash: Optional[str] = Field(
        None, description="Provide instead of `deliverable` if you hashed it yourself")
    deliverable_url: Optional[str] = Field(None, description="Optional pointer to the artifact")
    payment: float = Field(0.0, ge=0.0, description="Simulated payment for the work")
    stake: float = Field(0.0, ge=0.0, description="Reputation staked on the grade (simulated)")


class AttestationRequest(BaseModel):
    # Custodial path: identify issuer + authenticate, server signs the VC.
    issuer_id: Optional[str] = None
    subject_id: str = Field(..., description="Agent being attested to")
    capability: str = Field(..., description="What the work was, e.g. 'fact-check'")
    rating: float = Field(..., ge=0.0, le=1.0, description="Quality in [0,1]")
    task_id: str = Field("n/a", description="Receipt this attestation is evidence for")
    stake: float = Field(0.0, ge=0.0, description="Reputation staked on this claim (simulated)")
    comment: str = ""
    # Self-sovereign path: a pre-signed Verifiable Credential.
    credential: Optional[dict[str, Any]] = None


class AttestationResponse(BaseModel):
    id: str
    credential: dict[str, Any]
    verified: bool
    # Phase 1: journey guidance for the issuer (the authenticated party).
    guild_next: Optional[dict[str, Any]] = None


class ReputationResponse(BaseModel):
    # --- schema v2: trust is never a bare scalar (white paper §6.1) ---------
    # `estimate` (0..1) + `confidence` + `staleness` + `explanation` are the
    # v2 contract. `trust` (0..100) and `rank` remain for v1 callers and are
    # deprecated: same information, wrong shape.
    schema_version: int = 2
    estimate: float = 0.0                 # posterior point estimate, 0..1
    staleness: Optional[float] = None     # null until time-decay ships (stage 2)
    explanation: list[str] = Field(default_factory=list)
    agent_id: str
    did: str
    trust: float
    rank: int
    total_agents: int
    eigen_trust: float
    weighted_quality: float
    endorsement_accuracy: float
    confidence: float
    distinct_reviewers: int
    attestations_received: int
    # --- v0.2 evidence-based breakdown -------------------------------------
    raw_rating: float = 0.0
    verified_task_count: int = 0
    trusted_attestations: int = 0
    suspicious_attestations: int = 0
    backed_attestations: int = 0
    collusion_suspicion: float = 0.0
    slash_penalty: float = 0.0
    flag_reasons: list[str] = Field(default_factory=list)


class EvidenceAttestation(BaseModel):
    id: str
    issuer_id: str
    rating: float
    task_id: Optional[str] = None
    stake: float = 0.0
    verified: bool
    evidence_weight: float


class EvidenceReceipt(BaseModel):
    id: str
    requester: str
    task_type: str
    payment: float
    deliverable_hash: Optional[str] = None
    outcome: str


class EvidenceResponse(BaseModel):
    agent_id: str
    trust: float
    raw_rating: float
    verified_task_count: int
    trusted_attestations: int
    suspicious_attestations: int
    backed_attestations: int
    collusion_suspicion: float
    slash_penalty: float
    attestations: list[EvidenceAttestation]
    receipts: list[EvidenceReceipt]


class FlagResponse(BaseModel):
    agent_id: str
    suspicion: float
    reasons: list[str]
    cluster_id: Optional[int] = None


# --- billing ----------------------------------------------------------------
class AccountResponse(BaseModel):
    key: str
    balance: int
    spent: int
    topped_up: int
    owner_agent_id: Optional[str] = None
    credit_usd: float
    pricing: dict[str, int]


class TopupRequest(BaseModel):
    credits: int = Field(..., gt=0, description="Number of credits to add")
    # Dev/private-pilot path: mint credits directly with the dev token.
    dev_token: Optional[str] = Field(None, description="GUILD_BILLING_DEV_TOKEN, if set")
    # Live path: where Stripe should redirect after checkout.
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


class TopupResponse(BaseModel):
    mode: str                 # "dev" (credited now) or "stripe" (checkout)
    balance: Optional[int] = None
    checkout_url: Optional[str] = None
    usd: Optional[float] = None


class RiskScoreResponse(BaseModel):
    # v2: the Guild presents evidence; the ASKER decides. `recommendation` is
    # deprecated — a trust layer that says hire/avoid is making the caller's
    # decision for them (neutrality, white paper §8.8). Use estimate +
    # confidence + explanation and apply your own thresholds.
    schema_version: int = 2
    estimate: float = 0.0
    staleness: Optional[float] = None
    explanation: list[str] = Field(default_factory=list)
    agent_id: str
    name: str
    risk: float               # 0 (safe) .. 100 (risky) — deprecated, derive from estimate/confidence
    recommendation: str       # deprecated: hire | caution | avoid
    trust: float
    confidence: float
    collusion_suspicion: float
    verified_task_count: int
    trusted_attestations: int


class SearchResultItem(BaseModel):
    id: str
    did: str
    name: str
    capabilities: list[str]
    metadata: dict[str, Any]
    trust: float
    rank: int
    confidence: float
    attestations_received: int


class SearchResponse(BaseModel):
    capability: str
    count: int
    results: list[SearchResultItem]


# --- referrals (Outcome 1: agents as the growth engine) ---------------------
class ReferrerSummary(BaseModel):
    referrer_id: str
    name: Optional[str] = None
    referred: int          # how many agents this one referred
    activated: int         # how many of those activated (delivered/paid)
    rewarded_credits: int  # credits paid to this referrer so far


class ReferralsResponse(BaseModel):
    total_referrals: int
    activated_referrals: int
    activation_rate: Optional[float] = None
    rewarded_credits_total: int
    top_referrers: list[ReferrerSummary]


# --- self-evaluation (Outcome 4: continuous self-assessment) ----------------
class HealthSnapshot(BaseModel):
    at: str
    # utility — is the Guild actually helping agents?
    measured_lift: Optional[float] = None
    # provenance of measured_lift so the number never travels unlabelled:
    # "bootstrap" (seeded demonstration) | "production" | "mixed" | "empty".
    measured_lift_dataset: Optional[str] = None
    recommended_success_rate: Optional[float] = None
    # growth — are new (external) agents arriving?
    agents_total: int
    agents_external: int
    external_querying_agents: int = 0
    # retention — do external agents come back?
    external_repeat_query_agents: int
    external_repeat_paid_agents: int
    # revenue capture — is value being paid for?
    external_paid_queries: int
    credits_spent_external: int
    revenue_usd_external: float
    # referrals — are agents recruiting agents?
    total_referrals: int
    activated_referrals: int
    # deltas vs the previous snapshot (the trend, not the level)
    deltas: dict[str, float] = Field(default_factory=dict)
    verdict: str = ""


class HealthHistoryResponse(BaseModel):
    count: int
    snapshots: list[HealthSnapshot]
