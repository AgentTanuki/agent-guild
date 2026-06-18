"""Agent Guild — live public API (FastAPI).

A neutral trust layer for autonomous agents: register an identity (DID), issue
signed attestations about other agents, and read reputation computed from the
attestation graph. No blockchain, no payments, no tokens. Usable entirely via
HTTP.
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware

from .models import (
    RegisterRequest, RegisterResponse, AgentProfile,
    AttestationRequest, AttestationResponse,
    ReputationResponse, SearchResponse, SearchResultItem,
)
from .store import Store

app = FastAPI(
    title="Agent Guild",
    version="1.0.0",
    description="Portable, cryptographic reputation for autonomous AI agents.",
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

store = Store()
ADMIN_TOKEN = os.environ.get("GUILD_ADMIN_TOKEN", "")


def _profile(rec: dict) -> AgentProfile:
    return AgentProfile(
        id=rec["id"], did=rec["did"], name=rec["name"],
        capabilities=rec["capabilities"], metadata=rec["metadata"],
        public_key=rec["public_key"], seed=rec.get("seed", False),
        created_at=rec["created_at"],
        attestations_received=len(store.attestations_for(rec["id"])),
        attestations_issued=store.count_issued(rec["id"]),
    )


@app.get("/")
def root():
    return {
        "service": "Agent Guild",
        "version": "1.0.0",
        "endpoints": [
            "POST /agents/register", "GET /agents", "GET /agents/{id}",
            "GET /agents/{id}/reputation", "POST /attestations",
            "GET /agents/{id}/attestations", "GET /search?capability=x",
        ],
        "agents": len(store.agents),
        "attestations": len(store.attestations),
    }


@app.get("/health")
def health():
    return {"ok": True, "agents": len(store.agents), "attestations": len(store.attestations)}


@app.post("/agents/register", response_model=RegisterResponse)
def register(req: RegisterRequest, x_admin_token: Optional[str] = Header(None)):
    seed = req.seed
    if seed and ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "seed status requires a valid X-Admin-Token")
    rec = store.register_agent(
        name=req.name, capabilities=req.capabilities, metadata=req.metadata,
        public_key=req.public_key, seed=seed,
    )
    return RegisterResponse(
        id=rec["id"], did=rec["did"], public_key=rec["public_key"],
        capabilities=rec["capabilities"], api_key=rec.get("api_key"),
        custodial=rec["custodial"],
    )


@app.get("/agents")
def list_agents():
    return [
        {"id": a["id"], "did": a["did"], "name": a["name"],
         "capabilities": a["capabilities"], "seed": a.get("seed", False)}
        for a in store.agents.values()
    ]


@app.get("/agents/{agent_id}", response_model=AgentProfile)
def get_agent(agent_id: str):
    rec = store.get_agent(agent_id)
    if not rec:
        raise HTTPException(404, "agent not found")
    return _profile(rec)


@app.get("/agents/{agent_id}/reputation", response_model=ReputationResponse)
def get_reputation(agent_id: str):
    rec = store.get_agent(agent_id)
    if not rec:
        raise HTTPException(404, "agent not found")
    scores = store.reputation()
    s = scores.get(agent_id)
    if s is None:
        raise HTTPException(404, "no reputation computed")
    return ReputationResponse(
        agent_id=agent_id, did=rec["did"], trust=s.trust, rank=s.rank,
        total_agents=len(scores), eigen_trust=s.eigen_trust,
        weighted_quality=s.weighted_quality, endorsement_accuracy=s.endorsement_accuracy,
        confidence=s.confidence, distinct_reviewers=s.distinct_reviewers,
        attestations_received=s.attestations_received,
    )


@app.get("/agents/{agent_id}/attestations")
def get_attestations(agent_id: str):
    rec = store.get_agent(agent_id)
    if not rec:
        raise HTTPException(404, "agent not found")
    return store.attestations_for(agent_id)


@app.post("/attestations", response_model=AttestationResponse)
def post_attestation(req: AttestationRequest, x_api_key: Optional[str] = Header(None)):
    # Self-sovereign path: a pre-signed Verifiable Credential.
    if req.credential is not None:
        try:
            rec = store.add_signed_attestation(req.credential)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return AttestationResponse(id=rec["id"], credential=rec["credential"], verified=rec["verified"])

    # Custodial path: authenticate the issuer, Guild signs on its behalf.
    if not req.issuer_id:
        raise HTTPException(400, "issuer_id required (or provide a signed `credential`)")
    issuer = store.get_agent(req.issuer_id)
    if not issuer:
        raise HTTPException(404, "issuer not found")
    if not issuer.get("custodial"):
        raise HTTPException(400, "issuer is self-sovereign; submit a signed `credential` instead")
    if not x_api_key or x_api_key != issuer.get("api_key"):
        raise HTTPException(401, "invalid or missing X-API-Key for issuer")
    subject = store.get_agent(req.subject_id)
    if not subject:
        raise HTTPException(404, "subject not found")
    if req.subject_id == req.issuer_id:
        raise HTTPException(400, "an agent cannot attest to itself")
    rec = store.add_custodial_attestation(
        issuer, subject, req.capability, req.rating, req.task_id, req.comment,
    )
    return AttestationResponse(id=rec["id"], credential=rec["credential"], verified=rec["verified"])


@app.get("/search", response_model=SearchResponse)
def search(
    capability: str = Query(..., description="Capability to search for"),
    limit: int = Query(20, ge=1, le=200),
    min_trust: float = Query(0.0, ge=0.0, le=100.0),
):
    scores = store.reputation()
    items: list[SearchResultItem] = []
    for a in store.agents.values():
        if capability not in a["capabilities"]:
            continue
        s = scores.get(a["id"])
        trust = s.trust if s else 0.0
        if trust < min_trust:
            continue
        items.append(SearchResultItem(
            id=a["id"], did=a["did"], name=a["name"], capabilities=a["capabilities"],
            metadata=a["metadata"], trust=trust, rank=s.rank if s else 0,
            confidence=s.confidence if s else 0.0,
            attestations_received=s.attestations_received if s else 0,
        ))
    items.sort(key=lambda x: x.trust, reverse=True)
    return SearchResponse(capability=capability, count=len(items), results=items[:limit])
