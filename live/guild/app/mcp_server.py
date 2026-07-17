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

import contextvars
import json as _json
import os
from typing import Any, Callable, Optional
from typing_extensions import TypedDict

from fastmcp import Context, FastMCP
from fastmcp.tools.tool import ToolResult
from fastmcp.server.middleware import Middleware, MiddlewareContext

from x402.mcp.types import MCP_PAYMENT_META_KEY, MCP_PAYMENT_RESPONSE_META_KEY
from x402.schemas import PaymentPayload

from . import __version__
from . import callerproof
from . import demand
from . import journey as journey_engine
from . import payments
from . import proving
from . import x402
from .payments import CachedPaidResult, PaidRequest, PaymentChallenge, PaymentIdConflict
from .state import store
from . import credentials as _creds


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
    listing: dict           # your public listing URL + measured surface traffic


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


def _http_headers_for_attribution() -> dict:
    """The HTTP headers of the current MCP request (Streamable HTTP mount),
    lowercase keys — {} when unavailable (stdio/tests). Used ONLY for
    first-party payer attribution; never for authorization."""
    try:
        from fastmcp.server.dependencies import get_http_headers
        return {str(k).lower(): v for k, v in (get_http_headers() or {}
                                               ).items()}
    except Exception:
        return {}


def _first_party_payer() -> "bool | None":
    """Settle-time payer attribution for the MCP transport: True when the
    request carries valid token-gated first-party headers (the canary),
    otherwise None — an unclassified payer stays UNKNOWN, never external."""
    from . import firstparty as _fp_auth
    h = _http_headers_for_attribution()
    ok = _fp_auth.is_first_party(h.get(_fp_auth.HEADER.lower()),
                                 h.get(_fp_auth.LEGACY_HEADER.lower()))
    return True if ok else None


# --- caller-proof verification: ONE verification per tools/call --------------
# The agent-guild/caller-proof/v1 MCP mapping
# (_meta["io.agent-guild/caller-proof"]) is verified HERE, on the real
# execution path, exactly once per tool call, bound to:
#   method   = "tools/call"
#   resource = the exact MCP tool name
#   body     = sha256(JCS(visible tool arguments minus api_key/_meta))
# The (verified, did) outcome rides a request-scoped contextvar into demand
# recording (_record_mcp_demand) and settlement attribution (_serve_paid →
# payments.authorize). verify_proof consumes the envelope's nonce durably, so
# verifying once — and only once — per call is a correctness requirement:
# a second verification of the same envelope would be a nonce replay.

_mcp_caller_proof: contextvars.ContextVar[tuple[bool, str]] = \
    contextvars.ContextVar("mcp_caller_proof", default=(False, ""))


def _meta_value(meta: Any, key: str) -> Any:
    if meta is None:
        return None
    if isinstance(meta, dict):
        return meta.get(key)
    extra = getattr(meta, "model_extra", None)
    if isinstance(extra, dict):
        return extra.get(key)
    return None


def _extract_caller_proof_meta(context: "MiddlewareContext") -> Any:
    """The raw caller-proof envelope from the request _meta, or None.
    Checked on the message params first, then on the request context (where
    FastMCP surfaces client-supplied _meta for Streamable HTTP and
    in-process clients alike)."""
    env = _meta_value(getattr(context.message, "meta", None),
                      callerproof.MCP_META_KEY)
    if env is not None:
        return env
    try:
        rc_meta = context.fastmcp_context.request_context.meta
    except Exception:
        return None
    return _meta_value(rc_meta, callerproof.MCP_META_KEY)


class CallerProofMiddleware(Middleware):
    """Verify the caller-proof envelope for every tools/call — once."""

    async def on_call_tool(self, context: "MiddlewareContext", call_next):
        verified, did = False, ""
        try:
            env = _extract_caller_proof_meta(context)
            if env is not None:
                tool_name = str(getattr(context.message, "name", "") or "")
                arguments = getattr(context.message, "arguments", None) or {}
                out = callerproof.verify_proof(
                    store, env, method="tools/call", resource=tool_name,
                    body=callerproof.mcp_args_body(arguments))
                verified = bool(out.get("verified"))
                did = (out.get("did") or "") if verified else ""
        except Exception:
            # verification failure NEVER breaks a call — it stays unverified
            verified, did = False, ""
        token = _mcp_caller_proof.set((verified, did))
        try:
            return await call_next(context)
        finally:
            _mcp_caller_proof.reset(token)


def _caller_proof_state() -> tuple[bool, str]:
    """The (verified, did) outcome of THIS call's single verification."""
    return _mcp_caller_proof.get()


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

# one caller-proof verification per tools/call, on the REAL execution path.
mcp.add_middleware(CallerProofMiddleware())


def _rank(capability: str, limit: int, min_trust: float):
    # Shared ranking lives on the Store so MCP, /search and /check stay identical.
    return store.shortlist(capability, limit=limit, min_trust=min_trust)


# --- MCP paid-operation gate -------------------------------------------------
# The paid TRUST reads (guild_check / guild_search / guild_best_agent /
# guild_risk_score) used to record paid=false and serve the full payload free
# over MCP — a free cross-protocol bypass of the priced HTTP reads. They now
# route through the SAME shared gateway (app/payments.py) as HTTP and A2A: one
# semantic operation, one price, one enforcement policy on every transport.
#
# x402 over MCP: the official x402 MCP integration (x402.mcp) wraps servers
# built on the raw `mcp` SDK tool-handler signature; the Guild's hosted server
# is FastMCP-based, so there is no supported drop-in today. Rather than invent
# a proprietary pseudo-standard, the gate binds the MCP tool to the CANONICAL
# HTTP resource and speaks the official x402 MCP meta convention
# (MCP_PAYMENT_META_KEY 'x402/payment' in the request _meta; a v2
# PaymentRequired challenge as the tool error; MCP_PAYMENT_RESPONSE_META_KEY
# 'x402/payment-response' in the result _meta). An MCP client can therefore
# build the payment with the official x402 SDK, echo the canonical resource,
# and retry automatically — proven in tests/test_mcp_x402.py.


def _mcp_payment(ctx: "Context | None") -> Optional[PaymentPayload]:
    """Extract a v2 PaymentPayload from the MCP request _meta['x402/payment']
    (the official x402 MCP meta key). Returns None when absent/unusable."""
    if ctx is None or not x402.enabled():
        return None
    try:
        meta = ctx.request_context.meta
    except Exception:
        return None
    if meta is None:
        return None
    data = None
    extra = getattr(meta, "model_extra", None)
    if isinstance(extra, dict):
        data = extra.get(MCP_PAYMENT_META_KEY)
    if data is None:
        return None
    try:
        if isinstance(data, PaymentPayload):
            return data
        if isinstance(data, dict):
            return PaymentPayload(**data)
        if isinstance(data, str):
            return PaymentPayload(**_json.loads(data))
    except Exception:
        return None
    return None


def _challenge_result(body: dict[str, Any]) -> ToolResult:
    """A complete, machine-readable payment-required challenge as an MCP tool
    error — the unpaid caller never receives the paid payload."""
    return ToolResult(
        content=[{"type": "text", "text": _json.dumps(body, default=str)}],
        structured_content=body, is_error=True)


def _record_mcp_demand(capability: str, ctx: "Context | None",
                       api_key: str = "") -> "dict | None":
    """B1: the shared PRE-AUTHORIZATION demand recorder — invoked before
    authorize(), so an unpaid MCP caller's capability need is preserved even
    when the answer is a payment challenge. Carries the call's single
    caller-proof verification outcome: a valid proof records VERIFIED
    machine demand under the actor `did:<did>` (the UA heuristic plays no
    part); anything else stays the hashed heuristic actor."""
    import hashlib
    ua = _client_ua(ctx)
    verified, did = _caller_proof_state()
    if verified and did:
        actor = "did:" + did
    else:
        basis = ("key:" + api_key) if api_key else ("ua:" + ua)
        actor = "mcp:" + hashlib.sha256(
            ("agent-guild/demand-actor/" + basis).encode()).hexdigest()[:12]
    return demand.record_demand(capability, transport="mcp", actor=actor,
                                ua=ua, caller_proof_verified=verified,
                                caller_did=(did if verified else ""))


def _serve_paid(preq: PaidRequest, produce: Callable[[], Any],
                ctx: "Context | None", api_key: str = "",
                structured: bool = True,
                dem: "dict | None" = None) -> ToolResult:
    """Run one priced MCP read through the shared gateway. Returns the result
    ONLY on free/sandbox/settled authorization; an unpaid enforced call gets
    the challenge; a settled call carries the signed receipt + evidence in the
    result _meta under 'x402/payment-response'."""
    payment = _mcp_payment(ctx)
    ua = _client_ua(ctx)
    # the call's SINGLE caller-proof verification (middleware): its DID
    # feeds settlement attribution — the nonce is already consumed, never
    # re-verified here.
    verified, caller_did = _caller_proof_state()
    try:
        if payment is not None:
            # decode already done; authorize settles + binds to preq.
            # first_party: True for the token-authenticated canary, None
            # (unknown) otherwise — never affirmatively external.
            auth = payments.authorize(preq, payment=payment, protocol="v2",
                                      ua=ua, transport="mcp",
                                      first_party=_first_party_payer(),
                                      caller_did=(caller_did if verified
                                                  else ""))
        else:
            auth = payments.authorize(preq, api_key=(api_key or None),
                                      ua=ua, transport="mcp",
                                      caller_did=(caller_did if verified
                                                  else ""))
    except PaymentChallenge as e:
        body = dict(e.body)
        ns = demand.no_supply_block(dem) if dem else None
        if ns:
            body["no_supply"] = ns
        return _challenge_result(body)
    except PaymentIdConflict as e:
        return _challenge_result({"error": "payment_identifier_conflict",
                                  "reason": e.reason, "detail": e.detail,
                                  "payment_id": e.payment_id})
    except x402.PaymentBindingError as e:
        ch = PaymentChallenge(preq, extra={"error": "x402_payment_invalid",
                                           "reason": e.reason,
                                           "detail": e.detail[:300]})
        return _challenge_result(ch.body)
    except CachedPaidResult as e:
        # official idempotency: same id + same request → cached result, no
        # second settlement.
        result = e.result_json
        meta = {"x402/idempotent-replay": True}
        if e.settle_record:
            meta[MCP_PAYMENT_RESPONSE_META_KEY] = e.settle_record
        return ToolResult(
            content=[{"type": "text", "text": e.record["result_body"]}],
            structured_content=(result if isinstance(result, dict)
                                else {"result": result}),
            meta=meta)
    result = produce()
    body = _json.dumps(result, default=str)
    sc = result if isinstance(result, dict) else {"result": result}
    if auth.mode == "x402" and auth.settled is not None:
        fin = auth.settled.finalize(body.encode("utf-8"))
        return ToolResult(content=[{"type": "text", "text": body}],
                          structured_content=sc,
                          meta={MCP_PAYMENT_RESPONSE_META_KEY:
                                fin["settle_response"]})
    if auth.mode == "credits_sandbox":
        return ToolResult(content=[{"type": "text", "text": body}],
                          structured_content=sc,
                          meta={"x402/settlement-unit": "credits_sandbox"})
    # free (soft-launch / self): return the plain payload unchanged
    return result


@mcp.tool
def guild_check(capability: str, api_key: str = "", ctx: Context = None) -> dict:
    """START HERE. One call to vet a `capability` before you delegate: returns the
    best-evidenced agent with an evidence verdict — `estimate` (0-1), `confidence`,
    and a checkable `explanation` — plus a ranked shortlist, machine-checkable
    PROOF the Guild improves outcomes (provenance-labelled), and how to contribute
    back. Read estimate AND confidence together and apply your own risk threshold:
    a high estimate with low confidence means thin evidence.

    This is a PAID trust read (same price + policy as GET /check on every
    transport). When the rail is active, an unpaid call returns a complete
    x402 payment challenge for the canonical HTTP resource; retry with the
    payment in the request _meta['x402/payment'] (official x402 MCP meta key),
    or pass a funded `api_key` for SANDBOX credits (never revenue). Free while
    the service is in soft-launch.

    Example: guild_check(capability="fact-check")
    Returns {capability, best_agent, verdict, shortlist, proof, why_trust_this,
    how_to_contribute}. Use guild_search / guild_risk_score for finer control.
    """
    dem = _record_mcp_demand(capability, ctx, api_key)
    return _serve_paid(payments.check_request(capability),
                       lambda: store.check(capability, demand_recorded=True),
                       ctx, api_key, dem=dem)


@mcp.tool
def guild_search(capability: str, min_trust: float = 0.0, limit: int = 10,
                 api_key: str = "", ctx: Context = None):
    """Find agents that have a capability, ranked by attack-resistant trust.

    Use this to build a shortlist before delegating work. `min_trust` filters out
    low-trust agents (0-100); `limit` caps the list.

    PAID trust read (same price + policy as GET /search). Unpaid + enforced →
    x402 challenge for the canonical resource; pay via _meta['x402/payment'] or
    a funded `api_key` (sandbox credits). Free in soft-launch.

    Example: guild_search(capability="fact-check", min_trust=40, limit=5)
    Returns a ranked list of {id, name, trust, confidence, price_per_call, rank}.
    """
    dem = _record_mcp_demand(capability, ctx, api_key)
    return _serve_paid(payments.search_request(capability, limit, min_trust),
                       lambda: _rank(capability, limit, min_trust), ctx,
                       api_key, dem=dem)


@mcp.tool
def guild_best_agent(capability: str, min_trust: float = 0.0,
                     api_key: str = "", ctx: Context = None):
    """The single safest agent to delegate a `capability` to right now (or null
    if none qualify). Call this first, before hiring or delegating.

    PAID trust read (same price + policy as GET /search). Unpaid + enforced →
    x402 challenge; pay via _meta['x402/payment'] or a funded `api_key`
    (sandbox credits). Free in soft-launch.

    Example: guild_best_agent(capability="summarize")
    Returns one {id, name, trust, confidence, price_per_call, rank} or null.
    """
    def _best():
        top = _rank(capability, 1, min_trust)
        return top[0] if top else None
    dem = _record_mcp_demand(capability, ctx, api_key)
    return _serve_paid(payments.search_request(capability, 1, min_trust),
                       _best, ctx, api_key, dem=dem)


@mcp.tool
def guild_risk_score(agent_id: str, api_key: str = "",
                     ctx: Context = None):
    """The evidence view for one agent before trusting it with a task or payment:
    `estimate` (0-1 expected quality), `confidence` (how much trusted evidence
    backs it), a checkable `explanation`, and collusion suspicion. Apply YOUR OWN
    threshold — the Guild presents evidence; the asker decides.

    PAID trust read (same price + policy as GET /agents/{id}/risk-score). Unpaid
    + enforced → x402 challenge; pay via _meta['x402/payment'] or a funded
    `api_key` (sandbox credits). Free in soft-launch.

    Example: guild_risk_score(agent_id="agt_1a2b3c")
    Deprecated v1 fields (`risk`, `recommendation`, `trust`) are still returned.
    """
    rec = store.get_agent(agent_id)
    if not rec:
        return {"error": "agent not found"}

    def _risk():
        v = store.risk_for(agent_id)   # shared with /check and /risk-score
        if v is None:
            return {"error": "no reputation"}
        v["name"] = rec["name"]
        return v
    return _serve_paid(payments.risk_score_request(agent_id), _risk, ctx,
                       api_key)


@mcp.tool
def guild_register(name: str, capabilities: list[str],
                   ctx: Context = None) -> Registration:
    """Register this agent on Agent Guild so others can find and vouch for you.
    Free, and you only need to do it once.

    Returns {id, did, api_key, capabilities, next_step}. SAVE the api_key — it is
    secret and signs every attestation you write. Example:
    guild_register(name="Acme-Summarizer", capabilities=["summarize","translate"]).

    Then complete the proving rung (guild_prove → guild_prove_verify): the one
    journey step you can finish alone, on this visit, with no counterparty.
    """
    rec = store.register_agent(name=name, capabilities=capabilities,
                               metadata={}, ua=_client_ua(ctx))
    # R2: the proving rung is being offered as the next step — count the offer,
    # or offered→started drop-off is unmeasurable.
    if store.record_milestone(rec["id"], "prove_offered"):
        store._save()
    base = journey_engine.BASE
    return {"id": rec["id"], "did": rec["did"], "api_key": rec["api_key"],
            "capabilities": rec["capabilities"],
            "next_step": (
                f"Prove control now — guild_prove(agent_id=\"{rec['id']}\", "
                f"api_key=<your api_key>) then guild_prove_verify(...): records a "
                "guild-observed task + receipt on your record (journey stage 1→2), "
                "completable alone, today."),
            # R1: the same-session reward, in numbers — your listing is public
            # NOW, and this is the measured traffic on the surfaces it appears in.
            "listing": {
                "url": f"{base}/agents/{rec['id']}",
                "visible_now": True,
                "answer_surface_traffic": store.discovery_stats(),
            }}


def _prove_auth(agent: dict, api_key: str) -> Optional[dict]:
    """Mirror of the REST `_require_key` rule: custodial agents must present
    their api_key (presenting it IS the credential_control proof); self-sovereign
    agents are trusted to drive their own keys — the signature is the proof."""
    if agent.get("custodial") and not _creds.verify_agent_key(agent, api_key):
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
        result = proving.verify(store, agent, signature=(signature or None),
                                ua=_client_ua(ctx))
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
    issuer = store.agent_for_presented_key(issuer_api_key)
    if not issuer:
        return {"error": "invalid issuer api_key"}
    if not _creds.has_scope(issuer, "attest"):
        return _creds.scope_error(issuer, "attest")
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
    """Record a collaboration in one call after another agent did work for you:
    creates the task, content-addresses the deliverable, stores the graded
    receipt, and writes your attestation — one `mutual_attestation` entry (YOUR
    receipt-backed claim) in the canonical collaboration ledger. A record reaches
    the highest class ('guild_mediated') only with two-party or independent
    proof: escrow settlement (guild_escrow_open → guild_escrow_release), a
    worker-countersigned receipt, or a Guild-observed invocation. This is how the
    shared record of who-did-good-work-for-whom gets built.

    outcome is "accepted" | "disputed" | "rejected"; rating is 0..1. Authenticate
    with YOUR api_key (from guild_register). Pass the work product as `deliverable`
    (it's hashed for you) or a precomputed `deliverable_hash`.
    Example: guild_record(issuer_api_key="sk_...", worker_id="agt_9x",
    capability="summarize", outcome="accepted", rating=0.95, deliverable="...").
    """
    issuer = store.agent_for_presented_key(issuer_api_key)
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


# --------------------------------------------------------------------------
# Discovery Swarm: invocable utility capabilities as first-class MCP tools.
# Generated from the fixture-gated capability registry (app/swarm) so the MCP
# surface, REST /invoke, and A2A 'invoke:' messages stay identical. Guests are
# rate-limited by the gateway; every completion carries a signed provenance
# envelope. See /.well-known/ag-identities/index.json and /terms.json.
# --------------------------------------------------------------------------

def _swarm_invoke(capability_id: str, payload: dict, api_key: str, ctx) -> dict:
    from . import journey as journey_engine
    from .swarm import gateway
    from .swarm.router import ensure_built
    ensure_built()
    try:
        _status, body = gateway.invoke(
            store, capability_id, payload, x_api_key=(api_key or None),
            client_host="mcp", ua=_client_ua(ctx), first_party=False,
            base=journey_engine.BASE)
        return body
    except gateway.Denied as d:
        return {"ok": False, "denied": d.kind, **d.detail}


def _make_swarm_tool(cap):
    import json as _json

    def tool_fn(payload: dict, api_key: str = "", ctx: Context = None) -> dict:
        return _swarm_invoke(cap.id, payload, api_key, ctx)

    tool_fn.__name__ = "ag_" + cap.id.replace(".", "_")
    tool_fn.__doc__ = (
        f"{cap.summary}\n\n{cap.description}\n\n"
        f"Deterministic, fixture-verified, free for guests (rate-limited; pass "
        f"your Guild api_key to use your member budget). Returns the result plus "
        f"a Guild-signed provenance envelope.\n\n"
        f"`payload` MUST match this JSON Schema:\n"
        f"{_json.dumps(cap.input_schema)}\n\n"
        f"Output schema: {_json.dumps(cap.output_schema)}")
    return tool_fn


def _register_swarm_tools() -> None:
    from .swarm.capabilities import CAPABILITIES

    @mcp.tool
    def ag_capabilities(ctx: Context = None) -> dict:
        """List Agent Guild's invocable utility capabilities (the ag_* tools):
        id, version, summary, input/output JSON schemas, latency, guest terms.
        All deterministic and fixture-verified; guest invocation is free within
        rate limits and every completion returns a signed provenance envelope.
        Full identity documents: GET /.well-known/ag-identities/index.json."""
        from . import journey as journey_engine
        from .swarm.router import ensure_built
        from .swarm.identity import registry
        ensure_built()
        store.record_event("mcp", "swarm_index_fetch", ua=_client_ua(ctx))
        return registry.index(journey_engine.BASE)

    for _cap in CAPABILITIES.values():
        mcp.tool(_make_swarm_tool(_cap))


_register_swarm_tools()

# Streamable-HTTP ASGI app, mounted by main.py at /mcp (served at /mcp/).
#
# Host/Origin guard configuration — evidence and rationale in
# docs/discovery-swarm/evidence/mcp-421-host-guard.md. History:
#   * unpinned fastmcp picked up a release whose guard default rejected every
#     non-localhost Host with a bare "421 Misdirected Request", silently
#     breaking the entire production /mcp surface (initialize, tools/list AND
#     invocation) for all external clients — found 2026-07-10.
#   * e8749bd disabled the guard globally as an emergency unblock.
#   * Now: the NARROWEST supported production-safe configuration — an explicit
#     Host allowlist. fastmcp semantics (http.py:_allowed_hosts_for_scope)
#     always append loopback DEFAULT_HOSTS and the bound server host, so local
#     dev and tests keep working; any other Host gets 421 (defense-in-depth
#     against Host-header tricks) and cross-site browser Origins get 403
#     (no cookies are used on /mcp, so CSRF exposure was already nil).
# Override hosts via GUILD_PUBLIC_HOSTS (comma-separated) when a custom
# domain lands. Fall back for old fastmcp versions whose http_app() lacks
# the kwargs (those versions had no guard to configure).
PUBLIC_HOSTS = [h.strip() for h in os.environ.get(
    "GUILD_PUBLIC_HOSTS", "agent-guild-5d5r.onrender.com").split(",") if h.strip()]
try:
    # "auto" + explicit allowlists = the documented narrow mode: with explicit
    # allowed_hosts, the guard validates EVERY request against
    # PUBLIC_HOSTS + loopback DEFAULT_HOSTS + the bound server host.
    # (fastmcp 3.4.4 setting http_host_origin_protection defaults to False,
    # so allowed_hosts alone would configure a guard that never runs —
    # verified empirically before this change.)
    mcp_app = mcp.http_app(path="/", host_origin_protection="auto",
                           allowed_hosts=PUBLIC_HOSTS,
                           allowed_origins=[f"https://{h}" for h in PUBLIC_HOSTS])
except TypeError:  # fastmcp < 3.x: no guard, no kwarg
    mcp_app = mcp.http_app(path="/")
