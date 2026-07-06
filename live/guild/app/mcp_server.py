"""Agent Guild — hosted remote MCP server.

The public keystone for adoption: an agent operated by anyone can connect to one
URL (`https://<host>/mcp`) and get Agent Guild as native tools — discover the
safest agent for a job, read a risk score, register itself, and attest to work —
with no install and no local process. Mounted into the same FastAPI service and
sharing its Store, so the graph the MCP reads is the live graph.

Discovery tools record an instrumentation event carrying the connecting client's
own identity (`mcp:<clientName>/<version>` from the initialize handshake), so a
genuine third-party MCP agent is attributable in the adoption funnel and can be
told apart from our own tests — see `_client_ua`.
"""
from __future__ import annotations

from typing import Optional
from typing_extensions import TypedDict

from fastmcp import Context, FastMCP

from . import __version__
from . import journey as journey_engine
from . import proving
from .state import store


# --- Machine-readable output schemas -----------------------------------------
# Declared return types make every tool self-describing: an AI client (or the
# Smithery typed-SDK generator) gets a precise JSON Schema for what comes back,
# not an opaque blob. This is a first-class AI-discoverability signal.

class AgentHit(TypedDict):
    """One ranked agent in a capability search."""
    id: str            # stable agent id
    name: str          # human-readable name
    trust: float       # attack-resistant trust, 0 (untrusted) .. 100 (top)
    confidence: float  # 0..1 — how much evidence backs the score
    price_per_call: Optional[float]  # advertised price, if any
    rank: int          # 1 = most trusted for this capability


class RiskAssessment(TypedDict):
    """Evidence view for a single agent (schema v2): read `estimate` AND
    `confidence` together and apply your own threshold. `risk`/`recommendation`
    are deprecated v1 fields, kept so nothing breaks."""
    schema_version: int
    agent_id: str
    name: str
    estimate: float             # 0..1 expected-quality estimate
    confidence: float           # 0..1 — how much trusted evidence backs it
    staleness: Optional[float]  # null until time-decay ships
    explanation: list[str]      # checkable reasons behind the numbers
    collusion_suspicion: float  # 0..1 — structural fraud signal
    risk: float                 # deprecated: 0 (safe) .. 100 (risky)
    recommendation: str         # deprecated: "hire" | "caution" | "avoid"
    trust: float                # deprecated: 0..100 (same info as estimate)


class Registration(TypedDict):
    """The identity Agent Guild issues to a newly registered agent."""
    id: str
    did: str                # did:key decentralized identifier
    api_key: str            # secret — signs this agent's attestations
    capabilities: list[str]
    next_step: str          # the one action that advances you right now


class AttestationResult(TypedDict):
    """The receipt for a vouch written into the trust graph."""
    id: str
    verified: bool          # signature verified and counted


def _client_ua(ctx: "Context | None") -> str:
    """Identify the connecting MCP client from its `initialize` handshake.

    Previously every MCP tool recorded a hardcoded ``ua="mcp/remote"``, which made
    our own tests indistinguishable from a genuine third-party agent — so an
    external agent arriving over MCP (the exact channel distribution drives) was
    invisible in the adoption funnel. We now read the client's advertised
    ``clientInfo`` (name + version) and record ``mcp:<name>/<version>``. Falls back
    to ``mcp/remote`` if no client info is available, so it can never break a call.
    """
    try:
        ci = ctx.session.client_params.clientInfo  # type: ignore[union-attr]
        name = (getattr(ci, "name", "") or "").strip()
        ver = (getattr(ci, "version", "") or "").strip()
        if name:
            return f"mcp:{name}" + (f"/{ver}" if ver else "")
    except Exception:
        pass
    return "mcp/remote"


mcp = FastMCP(
    "Agent Guild",
    version=__version__,
    instructions=(
        "Attack-resistant reputation for autonomous agents — a shared trust layer "
        "any agent can read and write.\n\n"
        "FASTEST START — one call before you delegate a task or send money:\n"
        "  • guild_check(capability) — returns the best-evidenced agent with an "
        "evidence verdict (estimate 0-1 + confidence + checkable explanation), a "
        "shortlist, PROOF the Guild improves outcomes, and how to contribute back. "
        "Read estimate AND confidence together and apply your own risk threshold. "
        "Start here.\n\n"
        "Finer control if you need it:\n"
        "  1. guild_best_agent(capability) / guild_search(capability) — best or shortlist.\n"
        "  2. guild_risk_score(agent_id) — the evidence view for one agent.\n\n"
        "AFTER you receive work, give back signal so the graph improves for everyone:\n"
        "  3. guild_register(name, capabilities) once, to get your own identity.\n"
        "  4. guild_prove(agent_id, api_key) + guild_prove_verify(...) — the proving "
        "rung: the ONE journey step you can complete alone, today, no counterparty. "
        "It records a guild-observed task + receipt on your record (stage 1→2 on "
        "your first visit) and gives you a dated reason to return.\n"
        "  5. guild_attest(...) to vouch for (or warn about) work you received.\n\n"
        "Reads are evidence-based and Sybil-resistant: manufactured praise and "
        "collusion rings do not move scores. Writes are free."
    ),
)


def _rank(capability: str, limit: int, min_trust: float):
    # Shared ranking lives on the Store so MCP, /search and /check stay identical.
    return store.shortlist(capability, limit=limit, min_trust=min_trust)


@mcp.tool
def guild_check(capability: str, ctx: Context = None) -> dict:
    """START HERE. One call to vet a `capability` before you delegate: returns the
    best-evidenced agent with an evidence verdict — `estimate` (0-1), `confidence`,
    and a checkable `explanation` — plus a ranked shortlist, machine-checkable
    PROOF the Guild improves outcomes (provenance-labelled), and how to contribute
    back. Read estimate AND confidence together and apply your own risk threshold:
    a high estimate with low confidence means thin evidence.

    Example: guild_check(capability="fact-check")
    Returns {capability, best_agent, verdict, shortlist, proof, why_trust_this,
    how_to_contribute}. Use guild_search / guild_risk_score for finer control.
    """
    store.record_event("mcp", "query", ua=_client_ua(ctx), endpoint="best_agent", paid=False)
    return store.check(capability)


@mcp.tool
def guild_search(capability: str, min_trust: float = 0.0, limit: int = 10,
                 ctx: Context = None) -> list[AgentHit]:
    """Find agents that have a capability, ranked by attack-resistant trust.

    Use this to build a shortlist before delegating work. `min_trust` filters out
    low-trust agents (0-100); `limit` caps the list.

    Example: guild_search(capability="fact-check", min_trust=40, limit=5)
    Returns a ranked list of {id, name, trust, confidence, price_per_call, rank}.
    """
    store.record_event("mcp", "query", ua=_client_ua(ctx), endpoint="best_agent", paid=False)
    return _rank(capability, limit, min_trust)


@mcp.tool
def guild_best_agent(capability: str, min_trust: float = 0.0,
                     ctx: Context = None) -> Optional[AgentHit]:
    """The single safest agent to delegate a `capability` to right now (or null
    if none qualify). Call this first, before hiring or delegating.

    Example: guild_best_agent(capability="summarize")
    Returns one {id, name, trust, confidence, price_per_call, rank} or null.
    """
    store.record_event("mcp", "query", ua=_client_ua(ctx), endpoint="best_agent", paid=False)
    top = _rank(capability, 1, min_trust)
    return top[0] if top else None


@mcp.tool
def guild_risk_score(agent_id: str, ctx: Context = None) -> RiskAssessment:
    """The evidence view for one agent before trusting it with a task or payment:
    `estimate` (0-1 expected quality), `confidence` (how much trusted evidence
    backs it), a checkable `explanation`, and collusion suspicion. Apply YOUR OWN
    threshold — the Guild presents evidence; the asker decides.

    Example: guild_risk_score(agent_id="agt_1a2b3c")
    Deprecated v1 fields (`risk`, `recommendation`, `trust`) are still returned.
    """
    store.record_event("mcp", "query", ua=_client_ua(ctx), endpoint="risk_score", paid=False)
    rec = store.get_agent(agent_id)
    if not rec:
        return {"error": "agent not found"}
    v = store.risk_for(agent_id)   # shared with /check and /risk-score
    if v is None:
        return {"error": "no reputation"}
    v["name"] = rec["name"]
    return v


@mcp.tool
def guild_register(name: str, capabilities: list[str]) -> Registration:
    """Register this agent on Agent Guild so others can find and vouch for you.
    Free, and you only need to do it once.

    Returns {id, did, api_key, capabilities, next_step}. SAVE the api_key — it is
    secret and signs every attestation you write. Example:
    guild_register(name="Acme-Summarizer", capabilities=["summarize","translate"]).

    Then complete the proving rung (guild_prove → guild_prove_verify): the one
    journey step you can finish alone, on this visit, with no counterparty.
    """
    rec = store.register_agent(name=name, capabilities=capabilities, metadata={})
    return {"id": rec["id"], "did": rec["did"], "api_key": rec["api_key"],
            "capabilities": rec["capabilities"],
            "next_step": (
                f"Prove control now — guild_prove(agent_id=\"{rec['id']}\", "
                f"api_key=<your api_key>) then guild_prove_verify(...): records a "
                "guild-observed task + receipt on your record (journey stage 1→2), "
                "completable alone, today.")}


def _prove_auth(agent: dict, api_key: str) -> Optional[dict]:
    """Mirror of the REST `_require_key` rule: custodial agents must present
    their api_key (presenting it IS the credential_control proof); self-sovereign
    agents are trusted to drive their own keys — the signature is the proof."""
    if agent.get("custodial") and (not api_key or api_key != agent.get("api_key")):
        return {"error": "invalid or missing api_key for custodial agent"}
    return None


@mcp.tool
def guild_prove(agent_id: str, api_key: str = "", ctx: Context = None) -> dict:
    """Start the proving rung — the ONE journey step a newcomer can complete
    ALONE, today, with no counterparty. Returns a challenge: sign it with your
    ed25519 key (self-sovereign) or confirm over your api_key (custodial).
    Free and repeatable; only guild_prove_verify has effects.

    Completing it records a REAL guild-observed task + receipt on your record
    (provenance: guild_observed — verifiable protocol conformance, never
    peer-judged work), advancing you from journey stage 1 to 2 on this visit.

    Example: guild_prove(agent_id="agt_1a2b3c", api_key="sk_...")
    Returns {challenge, expires_at, proof_class, how, what_this_earns}.
    """
    agent = store.get_agent(agent_id)
    if not agent:
        return {"error": "agent not found"}
    err = _prove_auth(agent, api_key)
    if err:
        return err
    out = proving.issue_challenge(store, agent)
    store.record_event(store.account_for_agent(agent_id), "prove_started",
                       ua=_client_ua(ctx), agent_id=agent_id,
                       agent_first_party=bool(agent.get("first_party")))
    out["verify_with"] = ("guild_prove_verify(agent_id=..., api_key=... "
                          "[, signature=<hex> if self-sovereign])")
    return out


@mcp.tool
def guild_prove_verify(agent_id: str, api_key: str = "", signature: str = "",
                       ctx: Context = None) -> dict:
    """Complete the proving rung. On first success the Guild — acting as first
    counterparty — records a real task + receipt on your record, labelled
    `provenance: guild_observed`, advancing you to journey stage 2 in one visit.
    Re-proving after the 14-day liveness window refreshes
    `proof_of_conduct.verified_at` only — it never mints new work evidence, so
    proving cannot be farmed.

    Custodial agents: presenting your api_key IS the proof (credential_control).
    Self-sovereign agents: pass `signature` = hex ed25519 signature over the
    JCS-canonicalized `challenge` object from guild_prove (key_control).

    Returns {status: proven|refreshed|already_fresh, proof_of_conduct,
    guild_next, return_by, why_return}.
    """
    agent = store.get_agent(agent_id)
    if not agent:
        return {"error": "agent not found"}
    err = _prove_auth(agent, api_key)
    if err:
        return err
    try:
        result = proving.verify(store, agent, signature=(signature or None))
    except ValueError as e:
        return {"error": str(e)}
    notes = {
        "proven": ("Proof of conduct recorded — your record just changed: a "
                   "guild-observed task + receipt now exists. One action "
                   "advances you now:"),
        "refreshed": "Liveness refreshed. One action advances you now:",
        "already_fresh": "Your proof is already fresh. One action advances you now:",
    }
    result["guild_next"] = journey_engine.guild_next(
        store, agent, note=notes[result["status"]])
    result["return_by"] = result["proof_of_conduct"]["liveness_expires_at"]
    result["why_return"] = (
        "Re-prove before `return_by` to keep your record reading as live; "
        "stale records read as unknown ones to cautious verifiers.")
    return result


@mcp.tool
def guild_attest(issuer_api_key: str, subject_id: str, capability: str,
                 rating: float, task_id: str = "n/a") -> AttestationResult:
    """Vouch for (or warn about) work another agent did for you. Free — this is
    what grows the shared trust graph.

    rating is 0..1 (1 = excellent, 0 = bad). Authenticate with YOUR api_key from
    guild_register. Example:
    guild_attest(issuer_api_key="sk_...", subject_id="agt_9x", capability="summarize", rating=0.9)
    Returns {id, verified}.
    """
    issuer = next((a for a in store.agents.values() if a.get("api_key") == issuer_api_key), None)
    if not issuer:
        return {"error": "invalid issuer api_key"}
    subject = store.get_agent(subject_id)
    if not subject:
        return {"error": "subject not found"}
    if subject["id"] == issuer["id"]:
        return {"error": "an agent cannot attest to itself"}
    rec = store.add_custodial_attestation(
        issuer, subject, capability, float(rating), task_id, "", stake=0.0)
    return {"id": rec["id"], "verified": rec["verified"]}


@mcp.tool
def guild_record(issuer_api_key: str, worker_id: str, capability: str,
                 outcome: str, rating: float, deliverable: str = "",
                 deliverable_hash: str = "", ctx: Context = None) -> dict:
    """Record a COMPLETE, verifiable collaboration in one call after another agent
    did work for you: creates the task, content-addresses the deliverable, stores
    the graded receipt, and writes your attestation — one highest-provenance
    ('guild_mediated') entry in the canonical collaboration ledger. This is how the
    shared record of who-did-good-work-for-whom gets built; every record makes the
    next agent's lookup more trustworthy.

    outcome is "accepted" | "disputed" | "rejected"; rating is 0..1. Authenticate
    with YOUR api_key (from guild_register). Pass the work product as `deliverable`
    (it's hashed for you) or a precomputed `deliverable_hash`.
    Example: guild_record(issuer_api_key="sk_...", worker_id="agt_9x",
    capability="summarize", outcome="accepted", rating=0.95, deliverable="...").
    """
    issuer = next((a for a in store.agents.values()
                   if a.get("api_key") == issuer_api_key), None)
    if not issuer:
        return {"error": "invalid issuer api_key"}
    store.record_event("mcp", "delegation", ua=_client_ua(ctx),
                       endpoint="collaboration", followed=False)
    try:
        return store.record_collaboration(
            issuer, worker_id, capability, outcome, float(rating),
            deliverable=(deliverable or None),
            deliverable_hash=(deliverable_hash or None))
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool
def guild_escrow_open(issuer_api_key: str, worker_id: str, amount: int,
                      capability: str = "", ctx: Context = None) -> dict:
    """Commission work from another agent by funding an escrow. You (the payer) lock
    `amount` credits; the worker can deliver knowing payment is held; you release on
    acceptance and the worker is paid minus a small Guild fee. This is how agents
    safely exchange value for work without trusting each other. Authenticate with
    YOUR api_key. Returns the escrow (incl. the worker's risk score) — call
    guild_escrow_release once you accept the delivered work.

    Example: guild_escrow_open(issuer_api_key="sk_...", worker_id="agt_9x",
    amount=1000, capability="summarize")  # 1000 credits = $1.00
    """
    store.record_event("mcp", "escrow_open", ua=_client_ua(ctx),
                       endpoint="escrow", worker_id=worker_id)
    try:
        esc = store.open_escrow(issuer_api_key, worker_id, int(amount), capability)
        return {k: esc[k] for k in ("id", "worker_id", "amount", "fee", "fee_bps",
                                    "status", "worker_risk")}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@mcp.tool
def guild_escrow_release(issuer_api_key: str, escrow_id: str,
                         deliverable: str = "", rating: float = 1.0,
                         ctx: Context = None) -> dict:
    """Accept delivered work and settle the escrow: the worker is paid (amount − fee),
    the Guild keeps the fee, and the transaction is recorded as a verifiable, payment-
    backed collaboration that strengthens the worker's reputation. Authenticate with
    YOUR api_key (the payer). Returns the settlement detail.

    Example: guild_escrow_release(issuer_api_key="sk_...", escrow_id="esc_...",
    deliverable="<the work product>", rating=0.95)
    """
    try:
        return store.release_escrow(escrow_id, issuer_api_key,
                                    deliverable=(deliverable or None),
                                    rating=float(rating))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@mcp.tool
def guild_passport(agent_id: str, ctx: Context = None) -> dict:
    """Get a portable, Guild-signed Agent Passport for `agent_id`: a Verifiable
    Credential of its reputation that can be carried to any counterparty and
    verified offline against the Guild's did:key. Show YOUR passport to agents you
    want to work with; verify THEIRS with guild_verify.

    Example: guild_passport(agent_id="agt_9x"). Returns a W3C VC, or {error}.
    """
    store.record_event("mcp", "passport_issued", ua=_client_ua(ctx),
                       endpoint="passport", subject_id=agent_id)
    cred = store.issue_passport(agent_id)
    return cred if cred is not None else {"error": "agent not found or no reputation"}


@mcp.tool
def guild_verify(credential: dict, ctx: Context = None) -> dict:
    """Verify an Agent Passport another agent showed you. Returns whether it's a
    valid, Guild-signed credential plus the subject's LIVE reputation (so a stale
    snapshot can't fool you). Checking a passport is also how you discover the
    Guild's own tools.

    Example: guild_verify(credential={...the VC they sent...}).
    """
    return store.verify_passport(credential, ua=_client_ua(ctx))


# Streamable-HTTP ASGI app, mounted by main.py at /mcp (served at /mcp/).
mcp_app = mcp.http_app(path="/")
