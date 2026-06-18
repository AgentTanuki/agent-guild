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


class AttestationRequest(BaseModel):
    # Custodial path: identify issuer + authenticate, server signs the VC.
    issuer_id: Optional[str] = None
    subject_id: str = Field(..., description="Agent being attested to")
    capability: str = Field(..., description="What the work was, e.g. 'fact-check'")
    rating: float = Field(..., ge=0.0, le=1.0, description="Quality in [0,1]")
    task_id: str = "n/a"
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
