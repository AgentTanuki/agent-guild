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


class RegisterResponse(BaseModel):
    id: str
    did: str
    public_key: str
    capabilities: list[str]
    # Secret, returned once. Custodial agents use it to authenticate attestations.
    api_key: Optional[str] = None
    custodial: bool


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


class ReceiptRequest(BaseModel):
    deliverable_hash: str = Field(..., description="Hash of the delivered artifact")
    deliverable_url: Optional[str] = Field(None, description="Optional pointer to the artifact")
    outcome: str = Field("delivered", description="delivered | accepted | disputed | rejected")


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


class ReputationResponse(BaseModel):
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
    agent_id: str
    name: str
    risk: float               # 0 (safe) .. 100 (risky)
    recommendation: str       # hire | caution | avoid
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
