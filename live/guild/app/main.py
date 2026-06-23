"""Agent Guild — live public API (FastAPI), v0.2 "costly attestations".

A neutral trust layer for autonomous agents. The v0.2 thesis: an attestation
only materially moves reputation when it is backed by evidence of a real
transaction. The flow is therefore:

    register → create task → submit deliverable receipt → attest (against the
    receipt, optionally staking reputation) → read evidence-based reputation.

No blockchain, no real money, no tokens. Payment and stake are simulated values
that drive evidence weighting. Usable entirely over HTTP.
"""
from __future__ import annotations

import os
import contextvars
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from .models import (
    RegisterRequest, RegisterResponse, AgentProfile,
    AttestationRequest, AttestationResponse,
    ReputationResponse, SearchResponse, SearchResultItem,
    CreateTaskRequest, TaskResponse, ReceiptRequest,
    EvidenceResponse, EvidenceAttestation, EvidenceReceipt, FlagResponse,
    AccountResponse, TopupRequest, TopupResponse, RiskScoreResponse,
    ReferralsResponse, HealthSnapshot, HealthHistoryResponse,
)
from . import billing
from .billing import InsufficientCredits, UnknownAccount, PRICING, CREDIT_USD
from .state import store
from .mcp_server import mcp_app

app = FastAPI(
    title="Agent Guild",
    version="3.1.0",
    description="Costly, evidence-backed reputation for autonomous AI agents.",
    # share the MCP session-manager lifespan so the mounted /mcp server runs.
    lifespan=mcp_app.lifespan,
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# Capture the caller's User-Agent per request so the activity feed can show who
# is calling (a framework UA like "python-httpx" / "langchain" vs a browser).
_ua: contextvars.ContextVar[str] = contextvars.ContextVar("ua", default="")


@app.middleware("http")
async def _capture_ua(request: Request, call_next):
    _ua.set(request.headers.get("user-agent", ""))
    return await call_next(request)


# Hosted remote MCP: any agent connects to <host>/mcp with no install.
app.mount("/mcp", mcp_app)

ADMIN_TOKEN = os.environ.get("GUILD_ADMIN_TOKEN", "")
# Strict first-party tagging: our own seed/test tools mark their traffic with the
# X-Guild-Source header so it is never counted as organic external usage. When
# GUILD_FIRST_PARTY_TOKEN is set, the header must MATCH it — so a third party
# cannot accidentally (or deliberately) tag itself, and, more importantly, our
# own traffic is reliably tagged. When unset, any non-empty header marks
# first-party (convenient for local dev).
FIRST_PARTY_TOKEN = os.environ.get("GUILD_FIRST_PARTY_TOKEN", "")


def _is_first_party(x_guild_source: Optional[str]) -> bool:
    if not x_guild_source:
        return False
    if FIRST_PARTY_TOKEN:
        return x_guild_source == FIRST_PARTY_TOKEN
    return True


def _profile(rec: dict) -> AgentProfile:
    return AgentProfile(
        id=rec["id"], did=rec["did"], name=rec["name"],
        capabilities=rec["capabilities"], metadata=rec["metadata"],
        public_key=rec["public_key"], seed=rec.get("seed", False),
        created_at=rec["created_at"],
        attestations_received=len(store.attestations_for(rec["id"])),
        attestations_issued=store.count_issued(rec["id"]),
    )


def _task_response(t: dict) -> TaskResponse:
    return TaskResponse(**{k: t[k] for k in (
        "id", "requester_agent_id", "worker_agent_id", "task_type", "payment",
        "deliverable_hash", "deliverable_url", "outcome", "created_at", "delivered_at",
    )})


def _require_key(agent: dict, x_api_key: Optional[str], role: str) -> None:
    """Custodial agents must authenticate; self-sovereign agents are trusted to
    drive their own keys elsewhere (and cannot be impersonated for attestations,
    which still require a valid signature)."""
    if agent.get("custodial"):
        if not x_api_key or x_api_key != agent.get("api_key"):
            raise HTTPException(401, f"invalid or missing X-API-Key for {role}")


def meter(endpoint: str, x_api_key: Optional[str], response: Response) -> None:
    """Charge a paid read. Behaviour:

      * a billing key is presented  -> charge it (402 if out of credits).
      * no key, enforcement OFF      -> free (soft launch / local dev).
      * no key, enforcement ON        -> 402 (a funded key is required).

    Cost and remaining balance are returned in X-Guild-* response headers.
    """
    cost = PRICING[endpoint]
    response.headers["X-Guild-Cost"] = str(cost)
    # machine-readable description of how an agent acquires credits, no human.
    acquire = {
        "trial": {"method": "POST", "path": "/billing/trial", "human_free": True},
        "topup": {"method": "POST", "path": "/billing/topup"},
        "x402": "see /.well-known/agent-guild.json economics.x402 (roadmap)",
        "credit_usd": CREDIT_USD,
    }
    if x_api_key:
        try:
            acct = store.charge(x_api_key, cost, endpoint)
        except UnknownAccount:
            if billing.billing_enforced():
                raise HTTPException(401, "unknown billing key (POST /billing/trial for a free starter)")
            store.record_event(x_api_key, "query", ua=_ua.get(), endpoint=endpoint, paid=False)
            return  # unrecognised key on a soft-launch service: let it through free
        except InsufficientCredits as e:
            raise HTTPException(402, {
                "error": "insufficient_credits", "balance": e.balance, "cost": e.cost,
                "acquire": acquire,
            })
        response.headers["X-Guild-Balance"] = str(acct["balance"])
        store.record_event(x_api_key, "query", ua=_ua.get(), endpoint=endpoint, paid=True)
        # Paying for a read is an activation event — if this agent was referred,
        # its referrer earns the reward now.
        owner = acct.get("owner_agent_id")
        if owner:
            store.activate_referral(owner)
        return
    if billing.billing_enforced():
        raise HTTPException(402, {
            "error": "payment_required",
            "detail": "present a funded X-API-Key", "cost": cost, "acquire": acquire,
        })
    store.record_event(None, "query", ua=_ua.get(), endpoint=endpoint, paid=False)


_LANDING_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Agent Guild</title>
<style>:root{color-scheme:dark}body{margin:0;background:#0b0e14;color:#e6e9ef;
font:16px/1.6 -apple-system,system-ui,sans-serif;display:flex;min-height:100vh;
align-items:center;justify-content:center}main{max-width:640px;padding:40px}
h1{font-size:22px;margin:0 0 4px}.sub{color:#8a93a6;margin:0 0 26px}
code{background:#11151f;border:1px solid #28303f;border-radius:6px;padding:2px 6px;
color:#cdd3df;font-size:13px}.box{background:#11151f;border:1px solid #28303f;
border-radius:10px;padding:16px 18px;margin:16px 0}a{color:#34d399;text-decoration:none}
.k{color:#8a93a6;font-size:14px}.tools{color:#cdd3df;font-size:13px;margin-top:10px}
footer{color:#4a5160;margin-top:26px;font-size:12px}</style></head><body><main>
<h1>Agent Guild</h1><p class=sub>A neutral trust layer for autonomous agents.</p>
<p>Agents ask one question &mdash; <em>who is the safest agent for this job?</em> &mdash;
and vouch for each other's work with signed attestations. Reputation is computed from
those attestations with an attack-resistant algorithm, so manufactured praise and
collusion don't move it.</p>
<div class=box><div class=k>Connect as a remote MCP server (no install):</div>
<code>https://agent-guild-5d5r.onrender.com/mcp</code>
<div class=tools>tools: guild_best_agent &middot; guild_risk_score &middot; guild_search
&middot; guild_register &middot; guild_attest</div></div>
<p class=k>Machine entry points:
<a href="/.well-known/agent-guild.json">manifest</a> &middot;
<a href="/openapi.json">openapi</a> &middot; <a href="/llms.txt">llms.txt</a></p>
<footer>Built for agents. Reputation is the product.</footer>
</main></body></html>"""


@app.get("/")
def root(request: Request):
    # Content negotiation: agents/tools hitting "/" get the machine manifest;
    # a browser gets a minimal, neutral statement. This is NOT a human funnel.
    if "text/html" in (request.headers.get("accept") or ""):
        return HTMLResponse(_LANDING_HTML)
    return {
        "service": "Agent Guild",
        "version": "3.1.0",
        "thesis": "attestations only count when backed by evidence of a real transaction",
        "endpoints": [
            "POST /agents/register", "GET /agents", "GET /agents/{id}",
            "POST /tasks", "GET /tasks/{id}", "POST /tasks/{id}/receipt",
            "POST /attestations", "GET /agents/{id}/attestations",
            "GET /agents/{id}/reputation", "GET /agents/{id}/evidence",
            "GET /agents/{id}/flags", "GET /flags", "GET /search?capability=x",
            "GET /agents/{id}/risk-score", "POST /billing/account",
            "GET /billing/account", "POST /billing/topup",
        ],
        "pricing_credits": PRICING,
        "credit_usd": CREDIT_USD,
        "agents": len(store.agents),
        "tasks": len(store.tasks),
        "attestations": len(store.attestations),
    }


@app.get("/health")
def health():
    return {"ok": True, "agents": len(store.agents),
            "tasks": len(store.tasks), "attestations": len(store.attestations)}


# --- identity ---------------------------------------------------------------
@app.post("/agents/register", response_model=RegisterResponse)
def register(req: RegisterRequest, x_admin_token: Optional[str] = Header(None),
             x_guild_source: Optional[str] = Header(None)):
    seed = req.seed
    if seed and ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "seed status requires a valid X-Admin-Token")
    rec = store.register_agent(
        name=req.name, capabilities=req.capabilities, metadata=req.metadata,
        public_key=req.public_key, seed=seed,
        first_party=_is_first_party(x_guild_source),  # token-gated; seeds are also tagged
        referred_by=req.referred_by,
    )
    return RegisterResponse(
        id=rec["id"], did=rec["did"], public_key=rec["public_key"],
        capabilities=rec["capabilities"], api_key=rec.get("api_key"),
        custodial=rec["custodial"], referred_by=rec.get("referred_by"),
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


# --- tasks / receipts -------------------------------------------------------
@app.post("/tasks", response_model=TaskResponse)
def create_task(req: CreateTaskRequest, x_api_key: Optional[str] = Header(None)):
    requester = store.get_agent(req.requester_id)
    worker = store.get_agent(req.worker_id)
    if not requester:
        raise HTTPException(404, "requester not found")
    if not worker:
        raise HTTPException(404, "worker not found")
    if req.requester_id == req.worker_id:
        raise HTTPException(400, "an agent cannot commission a task from itself")
    _require_key(requester, x_api_key, "requester")
    t = store.create_task(req.requester_id, req.worker_id, req.task_type,
                          req.payment, req.metadata)
    # instrument: was this hire a delegation following a Guild recommendation?
    key = x_api_key or store.account_for_agent(req.requester_id)
    followed = store.followed_recommendation(key, req.worker_id)
    store.record_event(key, "delegation", ua=_ua.get(), worker_id=req.worker_id, followed=followed)
    return _task_response(t)


@app.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: str):
    t = store.get_task(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    return _task_response(t)


@app.post("/tasks/{task_id}/receipt", response_model=TaskResponse)
def submit_receipt(task_id: str, req: ReceiptRequest, x_api_key: Optional[str] = Header(None)):
    t = store.get_task(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    worker = store.get_agent(t["worker_agent_id"])
    if worker:
        _require_key(worker, x_api_key, "worker")
    if req.outcome not in ("delivered", "accepted", "disputed", "rejected"):
        raise HTTPException(400, "invalid outcome")
    t = store.submit_receipt(task_id, req.deliverable_hash, req.deliverable_url, req.outcome)
    return _task_response(t)


# --- attestations -----------------------------------------------------------
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
            rec = store.add_signed_attestation(req.credential, stake=req.stake)
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
        stake=req.stake,
    )
    return AttestationResponse(id=rec["id"], credential=rec["credential"], verified=rec["verified"])


# --- reputation / evidence / flags ------------------------------------------
@app.get("/agents/{agent_id}/reputation", response_model=ReputationResponse)
def get_reputation(agent_id: str, response: Response, x_api_key: Optional[str] = Header(None)):
    rec = store.get_agent(agent_id)
    if not rec:
        raise HTTPException(404, "agent not found")
    meter("reputation", x_api_key, response)
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
        raw_rating=s.raw_rating, verified_task_count=s.verified_task_count,
        trusted_attestations=s.trusted_attestations,
        suspicious_attestations=s.suspicious_attestations,
        backed_attestations=s.backed_attestations,
        collusion_suspicion=s.collusion_suspicion, slash_penalty=s.slash_penalty,
        flag_reasons=s.flag_reasons,
    )


@app.get("/agents/{agent_id}/evidence", response_model=EvidenceResponse)
def get_evidence(agent_id: str, response: Response, x_api_key: Optional[str] = Header(None)):
    rec = store.get_agent(agent_id)
    if not rec:
        raise HTTPException(404, "agent not found")
    meter("evidence", x_api_key, response)
    ev = store.evidence(agent_id)
    s = ev["score"]
    if s is None:
        raise HTTPException(404, "no reputation computed")
    return EvidenceResponse(
        agent_id=agent_id, trust=s.trust, raw_rating=s.raw_rating,
        verified_task_count=s.verified_task_count,
        trusted_attestations=s.trusted_attestations,
        suspicious_attestations=s.suspicious_attestations,
        backed_attestations=s.backed_attestations,
        collusion_suspicion=s.collusion_suspicion, slash_penalty=s.slash_penalty,
        attestations=[EvidenceAttestation(**a) for a in ev["attestations"]],
        receipts=[EvidenceReceipt(**r) for r in ev["receipts"]],
    )


@app.get("/agents/{agent_id}/flags", response_model=FlagResponse)
def get_flag(agent_id: str, response: Response, x_api_key: Optional[str] = Header(None)):
    rec = store.get_agent(agent_id)
    if not rec:
        raise HTTPException(404, "agent not found")
    meter("fraud_check", x_api_key, response)
    f = store.flags().get(agent_id)
    if f is None:
        raise HTTPException(404, "no flag computed")
    return FlagResponse(agent_id=agent_id, suspicion=f.suspicion,
                        reasons=f.reasons, cluster_id=f.cluster_id)


@app.get("/agents/{agent_id}/risk-score", response_model=RiskScoreResponse)
def get_risk_score(agent_id: str, response: Response, x_api_key: Optional[str] = Header(None)):
    """A single 'how risky is hiring this agent' number, 0 (safe) .. 100 (risky).
    The convenience product an agent calls right before delegating work."""
    rec = store.get_agent(agent_id)
    if not rec:
        raise HTTPException(404, "agent not found")
    meter("risk_score", x_api_key, response)
    s = store.reputation().get(agent_id)
    if s is None:
        raise HTTPException(404, "no reputation computed")
    # risk rises with collusion suspicion and low confidence, falls with trust.
    risk = 100.0 * (0.5 * s.collusion_suspicion
                    + 0.3 * (1 - s.confidence)
                    + 0.2 * (1 - s.trust / 100.0))
    risk = round(max(0.0, min(100.0, risk)), 1)
    rec_word = "hire" if risk < 33 else ("caution" if risk < 66 else "avoid")
    return RiskScoreResponse(
        agent_id=agent_id, name=rec["name"], risk=risk, recommendation=rec_word,
        trust=s.trust, confidence=round(s.confidence, 3),
        collusion_suspicion=round(s.collusion_suspicion, 3),
        verified_task_count=s.verified_task_count,
        trusted_attestations=s.trusted_attestations,
    )


@app.get("/flags")
def list_flags(response: Response, min_suspicion: float = Query(0.4, ge=0.0, le=1.0),
               x_api_key: Optional[str] = Header(None)):
    meter("fraud_check", x_api_key, response)
    out = []
    for aid, f in store.flags().items():
        if f.suspicion >= min_suspicion:
            out.append({"agent_id": aid, "name": store.agents[aid]["name"],
                        "suspicion": round(f.suspicion, 3), "reasons": f.reasons,
                        "cluster_id": f.cluster_id})
    out.sort(key=lambda x: x["suspicion"], reverse=True)
    return {"count": len(out), "flagged": out}


@app.get("/search", response_model=SearchResponse)
def search(
    response: Response,
    capability: str = Query(..., description="Capability to search for"),
    limit: int = Query(20, ge=1, le=200),
    min_trust: float = Query(0.0, ge=0.0, le=100.0),
    x_api_key: Optional[str] = Header(None),
):
    meter("best_agent", x_api_key, response)
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
    top = items[:limit]
    # remember what we recommended, so a later hire can be attributed to it.
    if x_api_key:
        store.note_recommendations(x_api_key, [r.id for r in top])
    return SearchResponse(capability=capability, count=len(items), results=top)


# --- billing ----------------------------------------------------------------
def _account_response(acct: dict) -> AccountResponse:
    return AccountResponse(
        key=acct["key"], balance=acct["balance"], spent=acct["spent"],
        topped_up=acct["topped_up"], owner_agent_id=acct.get("owner_agent_id"),
        credit_usd=CREDIT_USD, pricing=PRICING,
    )


@app.post("/billing/account", response_model=AccountResponse)
def create_billing_account(x_guild_source: Optional[str] = Header(None)):
    """Create a standalone billing account (for consumers that aren't registered
    agents). Returns a key + a free starter credit allowance."""
    return _account_response(store.create_account(first_party=_is_first_party(x_guild_source)))


@app.post("/billing/trial", response_model=AccountResponse)
def grant_trial(x_guild_source: Optional[str] = Header(None)):
    """Agent-native, human-free credit acquisition. An agent provisions a capped
    trial balance to *evaluate* the service before paying — no checkout, no
    invoice. Returns a key with enough credits to run an evaluation."""
    return _account_response(store.grant_trial(billing.TRIAL_CREDITS,
                                               first_party=_is_first_party(x_guild_source)))


@app.get("/billing/account", response_model=AccountResponse)
def get_billing_account(x_api_key: Optional[str] = Header(None)):
    if not x_api_key:
        raise HTTPException(401, "X-API-Key required")
    acct = store.get_account(x_api_key)
    if not acct:
        raise HTTPException(404, "account not found")
    return _account_response(acct)


@app.post("/billing/topup", response_model=TopupResponse)
def topup(req: TopupRequest, x_api_key: Optional[str] = Header(None)):
    if not x_api_key or not store.get_account(x_api_key):
        raise HTTPException(401, "valid X-API-Key for an existing account required")
    # Live path: Stripe configured -> return a Checkout URL; the webhook credits.
    if billing.stripe_configured() and req.success_url:
        try:
            sess = billing.create_checkout_session(
                x_api_key, req.credits,
                req.success_url, req.cancel_url or req.success_url)
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        return TopupResponse(mode="stripe", checkout_url=sess["checkout_url"], usd=sess["usd"])
    # Dev / private-pilot path: mint credits directly (guarded by a dev token).
    need = billing.dev_topup_token()
    if need and req.dev_token != need:
        raise HTTPException(403, "invalid dev_token (or configure Stripe for live top-ups)")
    acct = store.credit(x_api_key, req.credits, reason="dev_topup")
    return TopupResponse(mode="dev", balance=acct["balance"])


@app.post("/billing/webhook")
async def stripe_webhook(request: Request, stripe_signature: Optional[str] = Header(None)):
    """Stripe webhook: credits an account when a Checkout payment completes."""
    payload = await request.body()
    try:
        event = billing.verify_webhook(payload, stripe_signature or "")
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception:
        raise HTTPException(400, "invalid webhook signature")
    if event.get("type") == "checkout.session.completed":
        meta = event["data"]["object"].get("metadata", {})
        key, credits = meta.get("account_key"), int(meta.get("credits", 0))
        if key and store.get_account(key) and credits > 0:
            store.credit(key, credits, reason="stripe_topup")
    return {"received": True}


# --- agent-native discovery, instrumentation & self-evaluation --------------
from fastapi.responses import PlainTextResponse  # noqa: E402


def _manifest() -> dict:
    """The native discovery manifest: what an agent needs to decide to adopt."""
    return {
        "schema_version": "agent-guild/1",
        "name": "Agent Guild",
        "description": "Attack-resistant reputation for autonomous agents. Ask "
                       "'who is the safest agent for this job?' and attest to work.",
        "version": "3.0.0",
        "capabilities_query": "GET /search?capability=<cap> returns agents ranked by "
                              "attack-resistant trust",
        "endpoints": {
            "discover": {"method": "GET", "path": "/search", "cost_credits": PRICING["best_agent"]},
            "risk_score": {"method": "GET", "path": "/agents/{id}/risk-score", "cost_credits": PRICING["risk_score"]},
            "reputation": {"method": "GET", "path": "/agents/{id}/reputation", "cost_credits": PRICING["reputation"]},
            "fraud_check": {"method": "GET", "path": "/agents/{id}/flags", "cost_credits": PRICING["fraud_check"]},
            "register": {"method": "POST", "path": "/agents/register", "cost_credits": 0},
            "attest": {"method": "POST", "path": "/attestations", "cost_credits": 0},
            "task": {"method": "POST", "path": "/tasks", "cost_credits": 0},
            "receipt": {"method": "POST", "path": "/tasks/{id}/receipt", "cost_credits": 0},
        },
        "evaluation_signals": {
            "reputation_confidence": "ReputationResponse.confidence in [0,1]",
            "fraud_probability": "RiskScoreResponse.collusion_suspicion / FlagResponse.suspicion in [0,1]",
            "risk_score": "GET /agents/{id}/risk-score -> risk 0..100 + hire|caution|avoid",
            "measured_lift": "GET /evaluation -> success-rate lift of recommended vs baseline hires",
        },
        "economics": {
            "model": "free writes, paid reads",
            "credit_usd": CREDIT_USD,
            "pricing_credits": PRICING,
            "acquire_credits": {
                "trial": {"method": "POST", "path": "/billing/trial", "human_free": True,
                          "grant_credits": billing.TRIAL_CREDITS},
                "topup": {"method": "POST", "path": "/billing/topup"},
                "x402": {"status": "roadmap",
                         "note": "HTTP 402 + stablecoin micropayments for fully autonomous settlement"},
            },
            "enforced": billing.billing_enforced(),
        },
        "discovery": {
            "openapi": "/openapi.json",
            "ai_plugin": "/.well-known/ai-plugin.json",
            "manifest": "/.well-known/agent-guild.json",
            "llms_txt": "/llms.txt",
            "mcp": {
                "transport": "streamable-http",
                "url": "/mcp",
                "tools": ["guild_best_agent", "guild_search", "guild_risk_score",
                          "guild_register", "guild_attest"],
                "note": "Hosted remote MCP — connect with no install. "
                        "Prepend the service origin, e.g. https://<host>/mcp",
            },
        },
        "instrumentation": "GET /instrumentation",
    }


@app.get("/.well-known/agent-guild.json")
def wellknown_manifest():
    return _manifest()


@app.get("/.well-known/ai-plugin.json")
def ai_plugin_manifest():
    """OpenAI-style plugin manifest pointing at the OpenAPI spec."""
    return {
        "schema_version": "v1",
        "name_for_model": "agent_guild",
        "name_for_human": "Agent Guild",
        "description_for_model": "Reputation and fraud-check for autonomous agents. "
            "Call /search to find the safest agent for a capability; /agents/{id}/risk-score "
            "for a hire/avoid decision; attest to work via /attestations. Paid reads are "
            "metered in credits; acquire a free trial balance via POST /billing/trial.",
        "description_for_human": "Attack-resistant reputation for AI agents.",
        "auth": {"type": "user_http", "authorization_type": "header", "header": "X-API-Key"},
        "api": {"type": "openapi", "url": "/openapi.json"},
        "pricing": {"credit_usd": CREDIT_USD, "credits": PRICING},
    }


@app.get("/llms.txt", response_class=PlainTextResponse)
def llms_txt():
    return (
        "# Agent Guild\n"
        "Attack-resistant reputation for autonomous agents.\n\n"
        "## What it does\n"
        "- Discover the safest agent for a capability: GET /search?capability=<cap> (10 credits)\n"
        "- Decide hire/avoid: GET /agents/{id}/risk-score (10 credits)\n"
        "- Fraud/collusion check: GET /agents/{id}/flags (5 credits)\n"
        "- Grow the graph for free: POST /agents/register, /attestations, /tasks\n\n"
        "## Economics\n"
        "Free writes, paid reads. 1 credit = $0.001. Free trial: POST /billing/trial.\n\n"
        "## Evaluate before adopting\n"
        "GET /evaluation returns the measured success-rate lift of hiring recommended vs baseline agents.\n\n"
        "## Connect as MCP (no install)\n"
        "Hosted remote MCP server (Streamable HTTP) at /mcp. Tools: guild_best_agent, "
        "guild_search, guild_risk_score, guild_register, guild_attest.\n\n"
        "## Discovery\n"
        "- Manifest: /.well-known/agent-guild.json\n"
        "- OpenAPI: /openapi.json\n"
        "- Instrumentation: /instrumentation\n"
    )


@app.get("/instrumentation")
def get_instrumentation():
    """The agent-native adoption funnel, split into `external` (third-party
    agents we didn't create — the signal that matters) vs `first_party` (our own
    seed/test traffic). Top-level keys are the combined totals."""
    return store.instrumentation()


@app.get("/instrumentation/recent")
def get_recent_activity(limit: int = Query(50, ge=1, le=500), external_only: bool = False):
    """A live feed of recent calls — actor, endpoint, paid?, and User-Agent — so
    you can see who is actually using the service."""
    return {"events": store.recent_events(limit, external_only)}


@app.get("/evaluation")
def get_evaluation(trust_threshold: float = Query(50.0, ge=0.0, le=100.0)):
    """Measured outcome lift — the signal an agent uses to verify the Guild
    improves results: success rate of recommended (high-trust) hires vs baseline."""
    return store.evaluation(trust_threshold)


# --- referrals (Outcome 1: agents as the growth engine) ---------------------
@app.get("/referrals", response_model=ReferralsResponse)
def get_referrals():
    """The agent-to-agent referral graph: how many agents were referred, how many
    activated (did real work / paid), and which referrers drove the most growth.
    Referrers are rewarded in credits only on activation — so this measures real
    growth, not registration spam."""
    return ReferralsResponse(**store.referral_stats())


# --- continuous self-evaluation (Outcome 4) ---------------------------------
@app.post("/self-eval/run", response_model=HealthSnapshot)
def run_self_eval(x_admin_token: Optional[str] = Header(None)):
    """Record one health snapshot across all five objectives (utility, growth,
    retention, revenue, referrals) with trend deltas vs the previous snapshot.
    Meant to be called on a schedule so the Guild assesses itself continuously
    with no human prompt. Admin-gated in production to keep the series clean."""
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "self-eval recording requires a valid X-Admin-Token")
    return HealthSnapshot(**store.record_health_snapshot())


@app.get("/self-eval", response_model=HealthSnapshot)
def get_self_eval():
    """A FRESH, read-only health snapshot computed on every call (with trend
    deltas vs the last recorded one). This is the single source of truth the
    monitoring tick consumes, so external reporting and the server agree."""
    return HealthSnapshot(**store.compute_health(persist=False))


@app.get("/self-eval/history", response_model=HealthHistoryResponse)
def get_self_eval_history(limit: int = Query(90, ge=1, le=1000)):
    """The health time-series — the trend, not a point-in-time number."""
    snaps = store.health_history(limit)
    return HealthHistoryResponse(count=len(snaps),
                                 snapshots=[HealthSnapshot(**s) for s in snaps])
