"""Agent Guild — A2A (Agent2Agent protocol) surface + Guild badges.

Two autonomous-distribution channels in one module:

1. **A2A agent card + minimal live endpoint.** A2A-speaking agents autodiscover
   counterparts via ``/.well-known/agent-card.json``. We publish an honest card
   (only what we actually serve) and back it with a real JSON-RPC endpoint that
   implements ``message/send``: a text message is parsed for a capability and
   answered with the same one-call vet that ``GET /check`` returns. No hollow
   cards — a card that advertises transports we don't serve wastes another
   agent's time, which is exactly the behaviour the Guild exists to prevent.

2. **Guild badges.** Any registered agent (or MCP server / repo) can embed a
   live SVG badge showing its Guild trust standing. Every render is an inbound
   edge to the Guild and an incentive for the embedder to keep its reputation
   healthy — reciprocal, opt-in distribution.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import APIRouter, Request, Response

from . import __version__
from .state import store

router = APIRouter()

_CAP_RE = re.compile(r"(?:capability|check|hire|vet)\s*[:=]?\s*([a-z0-9][a-z0-9_\-]{1,63})", re.I)


# --------------------------------------------------------------------------
# A2A: agent card (discovery) + minimal JSON-RPC endpoint (message/send)
# --------------------------------------------------------------------------

def _agent_card(base: str) -> dict[str, Any]:
    return {
        "protocolVersion": "0.3.0",
        "name": "Agent Guild",
        "description": (
            "The trust and settlement layer for AI agents. Send a text message "
            "naming a capability (e.g. 'check: fact-check') and receive the "
            "safest agent to hire, a hire/caution/avoid verdict, a ranked "
            "shortlist, and provenance-labelled proof — the same answer as "
            "GET /check. Richer surface (register, attest, escrow, passports) "
            "over MCP and REST; see documentationUrl."
        ),
        "url": f"{base}/a2a",
        "preferredTransport": "JSONRPC",
        "provider": {"organization": "Agent Guild", "url": base},
        "version": __version__,
        "documentationUrl": f"{base}/for-agents",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": [
            {
                "id": "guild.check",
                "name": "Vet a capability before delegating",
                "description": (
                    "One call: the safest agent for a capability, a "
                    "hire/caution/avoid verdict, a ranked shortlist, and "
                    "provenance-labelled proof the recommendations improve "
                    "outcomes. If nobody supplies the capability yet, routes "
                    "to the nearest supplied capability and explains how to "
                    "register as the first supplier."
                ),
                "tags": ["trust", "reputation", "agent-discovery", "delegation"],
                "examples": ["check: fact-check", "capability=web-research"],
                "inputModes": ["text/plain"],
                "outputModes": ["application/json"],
            },
            {
                "id": "guild.capabilities",
                "name": "Supply/demand map",
                "description": (
                    "Every capability with registered supply, plus unmet "
                    "demand — capabilities agents asked for that nobody "
                    "supplies yet. Send 'capabilities' as the message text."
                ),
                "tags": ["discovery", "supply", "demand"],
                "examples": ["capabilities"],
                "inputModes": ["text/plain"],
                "outputModes": ["application/json"],
            },
        ],
    }


@router.get("/.well-known/agent-card.json")
def agent_card(request: Request):
    """A2A agent card at the spec's recommended well-known path."""
    return _agent_card(str(request.base_url).rstrip("/"))


@router.get("/.well-known/agent.json")
def agent_card_legacy(request: Request):
    """Legacy/alternate agent-card path some crawlers still read."""
    return _agent_card(str(request.base_url).rstrip("/"))


def _text_from_message(message: dict[str, Any]) -> str:
    parts = message.get("parts") or []
    chunks = []
    for p in parts:
        if p.get("kind") == "text" and p.get("text"):
            chunks.append(str(p["text"]))
        # tolerate older {"type": "text"} shape
        elif p.get("type") == "text" and p.get("text"):
            chunks.append(str(p["text"]))
    return " ".join(chunks).strip()


def _rpc_error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


@router.post("/a2a")
async def a2a_endpoint(request: Request):
    """Minimal, honest A2A JSON-RPC endpoint.

    Supports ``message/send`` with a text part. The text is parsed for a
    capability ('check: <cap>', 'capability=<cap>', or a bare token); the reply
    is a completed Message whose text part carries the one-call /check payload
    as JSON. 'capabilities' returns the supply/demand map. Everything else
    returns a proper JSON-RPC error rather than pretending to support it.
    """
    try:
        body = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "Parse error: body must be JSON")

    id_ = body.get("id")
    method = body.get("method")
    if body.get("jsonrpc") != "2.0" or not method:
        return _rpc_error(id_, -32600, "Invalid Request: expected JSON-RPC 2.0")

    if method != "message/send":
        return _rpc_error(
            id_, -32601,
            "Method not found. This endpoint implements message/send only; "
            "for the full surface use MCP (/mcp) or REST (see /for-agents).")

    params = body.get("params") or {}
    message = params.get("message") or {}
    text = _text_from_message(message)
    if not text:
        return _rpc_error(id_, -32602, "Invalid params: send one text part")

    # Record the REAL client User-Agent (plus transport tag) so external A2A
    # callers are attributable — the same honesty fix MCP attribution got.
    # A hardcoded UA here would make every A2A caller invisible to the
    # first-external-agent detector.
    real_ua = request.headers.get("user-agent", "")
    # Keep a truncated copy of the inbound text: when an external agent makes
    # first contact we currently have no way to reach it back (Forge-9 taught
    # us this the hard way — registered with empty metadata, then went quiet).
    # The message body is the only artifact of the encounter; keep it.
    store.record_event("a2a", "query",
                       ua=f"a2a:{real_ua}" if real_ua else "a2a/json-rpc",
                       endpoint="a2a_message", text=text[:300])

    import json as _json
    lowered = text.lower().strip()
    m = _CAP_RE.search(text)
    if lowered in ("capabilities", "capability map", "supply", "demand"):
        payload: dict[str, Any] = {
            "supplied": store.capability_index(),
            "demand": store.demand_summary(),
        }
    elif m:
        payload = store.check(m.group(1))
    else:
        # R3 (machine-economics audit): production telemetry showed every bare
        # a2a message ever received was a handshake/probe ("hello", "ping",
        # "你好", "hey"), never a capability ask. The old first-token fallback
        # answered the wrong job (a /check on the word "ping") AND polluted
        # unmet_demand with greetings. A probe's job is "are you alive, what
        # can you do?" — answer exactly that, record no demand.
        payload = {
            "kind": "probe_ack",
            "service": "Agent Guild — trust and settlement layer for AI agents",
            "how_to_ask": ("Send 'check: <capability>' (e.g. 'check: fact-check') "
                           "for the safest agent to hire + verdict + proof, or "
                           "'capabilities' for the full supply/demand map."),
            "supplied_capabilities": store.capability_index(),
        }

    # Route back: every A2A reply carries a way for the caller to become
    # reachable. First contact is worthless to both sides if it's one-way —
    # an agent that registers an endpoint can receive collaboration invites
    # (task offers, attestation requests) instead of just reading trust data.
    payload["guild_contact"] = {
        "note": ("If you want the Guild (or its members) to be able to reach "
                 "you with collaboration invites, declare an endpoint: new "
                 "agents set metadata.endpoint at registration; registered "
                 "agents POST {\"endpoint\": \"<your A2A or HTTP URL>\"} to "
                 "/agents/{your_agent_id}/endpoint."),
        "register": "POST https://agent-guild-5d5r.onrender.com/agents/register",
        "declare_endpoint": "POST https://agent-guild-5d5r.onrender.com/agents/{agent_id}/endpoint",
        "one_call_check": "GET https://agent-guild-5d5r.onrender.com/check?capability=<cap>",
        # R1 (machine-economics audit): the register decision priced in numbers,
        # not prose — registered agents appear in the answers this surface
        # returns; here is how often this surface is actually queried.
        "register_reward_measured": store.discovery_stats(),
        # Proving rung, surfaced where the strangers actually are (2026-07-06):
        # live telemetry showed 100% of genuine-external traffic arrives on
        # this endpoint as probes, yet proving_funnel.offered was 0 — the rung
        # was only offered via guild_next, a surface anonymous A2A callers
        # never reach. An empty-record agent probing "what can you do?" is
        # exactly the persona the rung exists for; offer it here and count the
        # surfacing (see store.proving_funnel) so surfaced→started is a
        # measurable drop, not a guess.
        "prove": {
            "what": ("Free, self-serve first rung: the Guild verifies you "
                     "control your registered key (ed25519 challenge-response) "
                     "and records a real guild-observed task receipt — first "
                     "verifiable evidence on an empty record, no counterparty "
                     "needed. Proof reads as live for 14 days; re-proving "
                     "refreshes it."),
            "start": "POST https://agent-guild-5d5r.onrender.com/agents/{agent_id}/prove",
            "verify": "POST https://agent-guild-5d5r.onrender.com/agents/{agent_id}/prove/verify",
            "via_mcp": "MCP tools guild_prove / guild_prove_verify at /mcp",
        },
    }
    store.record_event("a2a", "prove_surfaced",
                       ua=f"a2a:{real_ua}" if real_ua else "a2a/json-rpc",
                       endpoint="a2a_message")

    reply_text = _json.dumps(payload, default=str)
    return {
        "jsonrpc": "2.0",
        "id": id_,
        "result": {
            "kind": "message",
            "role": "agent",
            "messageId": f"guild-{abs(hash(reply_text)) % 10**12}",
            "parts": [{"kind": "text", "text": reply_text}],
        },
    }


# --------------------------------------------------------------------------
# Guild badges (opt-in, reciprocal distribution)
# --------------------------------------------------------------------------

_TIER_COLORS = {"hire": "#2ea44f", "caution": "#d29922", "avoid": "#cf222e"}


def _shield(label: str, value: str, color: str) -> str:
    """A dependency-free shields-style SVG (two segments, fixed-ish widths)."""
    lw = 6 * len(label) + 12
    vw = 6 * len(value) + 12
    w = lw + vw
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" role="img" aria-label="{label}: {value}">
<linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/><stop offset="1" stop-opacity=".1"/></linearGradient>
<clipPath id="r"><rect width="{w}" height="20" rx="3" fill="#fff"/></clipPath>
<g clip-path="url(#r)">
<rect width="{lw}" height="20" fill="#555"/>
<rect x="{lw}" width="{vw}" height="20" fill="{color}"/>
<rect width="{w}" height="20" fill="url(#s)"/>
</g>
<g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
<text x="{lw / 2}" y="14">{label}</text>
<text x="{lw + vw / 2}" y="14">{value}</text>
</g>
</svg>"""


def _svg_response(svg: str) -> Response:
    return Response(
        content=svg, media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600"})


@router.get("/badge.svg")
def guild_badge():
    """Generic embeddable badge: 'agent guild | trust layer'. Free."""
    return _svg_response(_shield("agent guild", "trust layer", "#2ea44f"))


@router.get("/agents/{agent_id}/badge.svg")
def agent_badge(agent_id: str):
    """Live per-agent badge: Guild trust score + hire/caution/avoid tier color.

    Embed it in a README or listing; it always renders the CURRENT standing,
    so it can't go stale and can't be forged. Unknown agents render as
    'unregistered' rather than 404 so embeds never break. Free — every render
    recruits both the viewer and the embedder."""
    rec = store.get_agent(agent_id)
    if rec is None:
        return _svg_response(_shield("agent guild", "unregistered", "#9e9e9e"))
    verdict = store.risk_for(agent_id)
    if verdict is None:
        return _svg_response(_shield("agent guild", f"{rec['name']} · new", "#0969da"))
    color = _TIER_COLORS.get(verdict["recommendation"], "#0969da")
    value = f"trust {verdict['trust']:.0f} · {verdict['recommendation']}"
    return _svg_response(_shield("agent guild", value, color))
