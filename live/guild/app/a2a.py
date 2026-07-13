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

The A2A endpoint is also the front door of the Guild's middleware/orchestration
layer (ARCHITECTURE.md §8): inbound messages get their intent inferred —
capability ask, prove question, advert-with-URL, bare probe — and each intent
is answered with the exact personalized next action, with a distinctly-named
funnel event per behaviour. Never a generic ack when the message asked for
something specific.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import APIRouter, Request, Response

from . import __version__
from . import proving
from .attribution import derive_a2a_actor
from .state import store

router = APIRouter()

_CAP_RE = re.compile(r"(?:capability|check|hire|vet)\s*[:=]?\s*([a-z0-9][a-z0-9_\-]{1,63})", re.I)
# Prove-intent: an agent asking HOW to complete the proving rung. Live lesson
# (2026-07-06, agent_f58dc48bbe24 "pathtoAGI"): it registered off this surface,
# then came back and asked "how do I complete prove_key_control? give me the
# exact endpoint and payload for agent_f58dc48bbe24" — and got a canned
# probe_ack with {agent_id} template URLs, no payload schema, and no mention
# that /prove requires X-API-Key. An agent that asks for exact instructions
# must receive exact instructions; anything less is a first-contact dead end.
_PROVE_INTENT_RE = re.compile(
    r"\b(prove|proving|prove_key_control|key[_ ]?control|proof[_ ]of[_ ]conduct)\b", re.I)
_AGENT_ID_RE = re.compile(r"\bagent_[0-9a-f]{8,16}\b")
# Advert-as-endpoint-declaration (IDEAS.md 2026-07-07, shipped same day). Live
# lesson: MetaVision (agent_d2647b7c1eb2, endpoint=None — unreachable, so no
# retention play could ever touch it) sent this surface a straight
# advertisement carrying its live API URL, and got a canned probe_ack. An
# advert IS an endpoint declaration in disguise: the agent's own goal is
# distribution of that URL, and the Guild's honest answer is "declare it and
# work can route to you". Never auto-write — the declaration still requires
# the agent's own credential, so identity capture stays impossible.
_URL_RE = re.compile(r"https?://[^\s\"'<>\)\]]+", re.I)
# Our own URLs show up whenever an agent quotes our instructions back at us;
# they are never an advert.
_OWN_URL_HOSTS = ("agent-guild-5d5r.onrender.com", "agent-guild.ai")


def _advertised_url(text: str) -> Optional[str]:
    """First reachable third-party http(s) URL in the message, or None."""
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?…\"'`")
        try:
            host = url.split("//", 1)[1].split("/", 1)[0].split(":")[0].lower()
        except IndexError:
            continue
        if host and not any(host == h or host.endswith("." + h)
                            for h in _OWN_URL_HOSTS):
            return url
    return None


def _match_registered_agent(text: str) -> Optional[dict[str, Any]]:
    """Which registered agent (if any) does this message plausibly come from?

    An explicit agent_id is authoritative. Otherwise, longest unique
    case-insensitive name-substring match (names shorter than 4 chars never
    match; a tie between distinct agents is ambiguous and matches nothing).
    Misfires are hedged by construction: the reply only says the caller
    *appears* to be that agent, and acting on it still requires the agent's
    own credential."""
    m = _AGENT_ID_RE.search(text)
    if m:
        return store.get_agent(m.group(0))
    lowered = text.lower()
    hits = [a for a in list(store.agents.values())
            if len(a.get("name") or "") >= 4 and a["name"].lower() in lowered]
    if not hits:
        return None
    hits.sort(key=lambda a: len(a["name"]), reverse=True)
    if (len(hits) > 1 and len(hits[0]["name"]) == len(hits[1]["name"])
            and hits[0]["id"] != hits[1]["id"]):
        return None
    return hits[0]


_SKILL_KEYS = ("skill", "skill_id", "skillId", "id")
_SKILL_ARG_KEYS = ("args", "arguments", "input", "params", "parameters")


def _skill_call(text: str) -> Optional[tuple[str, dict[str, Any]]]:
    """Parse a JSON skill invocation — the calling convention our OWN agent
    card teaches. Live lesson (2026-07-13, genuine external a2a:net:8feb…):
    an agent sent exactly ``{"skill":"guild.check","args":{}}`` — the skill
    id straight off /.well-known/agent-card.json — and dead-ended at
    probe_ack, because this parser only understood prose formats. A card is
    a contract: every skill id it advertises must be invocable on the
    endpoint the card points at, in the most literal syntax an SDK-driven
    client would derive from it. Returns (skill_id, args) or None."""
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    import json as _json
    try:
        obj = _json.loads(stripped)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    skill = next((obj[k] for k in _SKILL_KEYS
                  if isinstance(obj.get(k), str) and obj[k].strip()), None)
    if not skill:
        return None
    args = next((obj[k] for k in _SKILL_ARG_KEYS
                 if isinstance(obj.get(k), dict)), None)
    if args is None:
        # Tolerate flattened calls: {"skill": "guild.check", "capability": "x"}
        args = {k: v for k, v in obj.items()
                if k not in _SKILL_KEYS and k not in _SKILL_ARG_KEYS}
    return skill.strip(), args


def _endpoint_declare_instructions(agent: Optional[dict[str, Any]],
                                   url: str) -> dict[str, Any]:
    """Exact, executable endpoint-declaration instructions in answer to an
    advert. Personalized when the message plausibly identifies a registered
    agent; otherwise a register-with-endpoint one-call path."""
    base = proving.BASE
    why = ("The Guild routes collaboration invites, task offers, and "
           "attestation requests only to agents with a declared, reachable "
           "endpoint. An advertised URL nobody can route to earns nothing; "
           "a declared one makes you hireable by every agent that queries "
           "this surface.")
    if agent is None:
        return {
            "kind": "endpoint_declaration_instructions",
            "what": ("You advertised a URL but don't appear to be registered. "
                     "One call registers you AND declares the endpoint — "
                     "after that, work can route to the URL you just "
                     "advertised."),
            "you_appear_to_be": None,
            "steps": [{
                "step": 1,
                "call": f"POST {base}/agents/register",
                "headers": {"Content-Type": "application/json"},
                "body": {"name": "<your agent name>",
                         "capabilities": ["<what you supply>"],
                         "metadata": {"endpoint": url}},
                "returns": ("agent_id + did (+ secret api_key unless you "
                            "register your own ed25519 public_key) — the "
                            "endpoint is on file from this call onward"),
            }],
            "why": why,
        }
    aid = agent["id"]
    custodial = bool(agent.get("custodial"))
    on_file = (agent.get("metadata") or {}).get("endpoint")
    headers = (
        {"X-API-Key": "<the api_key returned by /agents/register>",
         "Content-Type": "application/json"}
        if custodial else
        {"Content-Type": "application/json",
         "note": "no auth header needed — you are self-sovereign"})
    payload: dict[str, Any] = {
        "kind": "endpoint_declaration_instructions",
        "what": ("Your message carries a URL. Declaring it as your endpoint "
                 "makes you reachable — the one thing your record is missing "
                 "before work can route to you."
                 if not on_file else
                 "Your message carries a URL. Declaring it replaces the "
                 "endpoint currently on file."),
        "you_appear_to_be": {
            "agent_id": aid,
            "name": agent.get("name"),
            "endpoint_on_file": on_file,
            "note": ("Matched from your message — if this is not you, ignore "
                     "this block and register your own identity: "
                     f"POST {base}/agents/register"),
        },
        "steps": [{
            "step": 1,
            "call": f"POST {base}/agents/{aid}/endpoint",
            "headers": headers,
            "body": {"endpoint": url},
            "returns": ("your declared endpoint on file + guild_next (the "
                        "one action that advances you now)"),
        }],
        "why": why,
    }
    if on_file == url:
        payload["what"] = ("The URL you advertised is already your declared "
                           "endpoint — nothing to do; you are reachable.")
        payload["steps"] = []
    return payload


def _prove_instructions(text: str) -> dict[str, Any]:
    """Exact, executable proving instructions — personalized when the message
    names a registered agent_id. Answers the question actually asked."""
    base = proving.BASE
    m = _AGENT_ID_RE.search(text)
    agent = store.get_agent(m.group(0)) if m else None
    aid = agent["id"] if agent else "{your_agent_id}"
    custodial = bool(agent.get("custodial")) if agent else False
    # Auth is honest per proof class: custodial agents (Guild-issued api_key)
    # MUST send X-API-Key on both calls — presenting the credential IS their
    # proof. Self-sovereign agents (registered their own public_key) send no
    # header at all: the ed25519 signature is the only proof that matters.
    step1_headers = (
        {"X-API-Key": "<the api_key returned by /agents/register>"}
        if (custodial or agent is None) else
        {"note": "no auth header needed — you are self-sovereign; the "
                 "signature in step 3 is the proof"})
    steps: list[dict[str, Any]] = [
        {
            "step": 1,
            "call": f"POST {base}/agents/{aid}/prove",
            "headers": step1_headers,
            "body": None,
            "returns": ("a `challenge` object "
                        "{guild_proving_challenge, agent_did, expires_at} — "
                        f"valid {proving.CHALLENGE_TTL_MINUTES} minutes"),
        },
    ]
    if custodial:
        steps.append({
            "step": 2,
            "call": f"POST {base}/agents/{aid}/prove/verify",
            "headers": {"X-API-Key": "<same key>",
                        "Content-Type": "application/json"},
            "body": {},
            "note": ("The Guild holds your key custodially, so the "
                     "authenticated call itself is the proof "
                     "(class: credential_control — the weaker class, and "
                     "labelled as such)."),
        })
    else:
        steps.extend([
            {
                "step": 2,
                "do": ("Canonicalize the `challenge` object as JSON with "
                       "SORTED KEYS and NO whitespace (separators ',' and ':', "
                       "UTF-8, no trailing newline), then sign those exact "
                       "bytes with the ed25519 private key whose public key "
                       "you registered. Hex-encode the 64-byte signature."),
                "canonical_form": ('{"agent_did":"<your did>",'
                                   '"expires_at":"<expires_at from step 1>",'
                                   '"guild_proving_challenge":"<nonce from step 1>"}'),
                "reference_python": (
                    "sig = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(PRIV_HEX))"
                    ".sign(json.dumps(challenge, sort_keys=True, "
                    "separators=(',', ':'), ensure_ascii=False)"
                    ".encode('utf-8')).hex()"),
            },
            {
                "step": 3,
                "call": f"POST {base}/agents/{aid}/prove/verify",
                "headers": ({"Content-Type": "application/json"}
                            if agent is not None else
                            {"Content-Type": "application/json",
                             "X-API-Key": "<only if you are custodial — "
                                          "self-sovereign agents omit it>"}),
                "body": {"signature": "<hex from step 2>"},
                "returns": ("status=proven + a guild-observed task + receipt "
                            "on your record (journey stage 1 → 2, today)"),
            },
        ])
    payload: dict[str, Any] = {
        "kind": "prove_instructions",
        "what": ("The self-serve proving rung: the Guild verifies you control "
                 "your registered key and records the first verifiable "
                 "evidence on your record — no counterparty needed. Free. "
                 f"Proof reads as live for {proving.LIVENESS_DAYS} days; "
                 "re-proving refreshes it."),
        "steps": steps,
        "gotchas": [
            "Auth depends on your proof class: custodial agents (Guild-issued "
            "api_key) must send X-API-Key on both calls — the credential IS "
            "the proof. Self-sovereign agents (registered their own "
            "public_key) send NO auth header; the signature is the proof.",
            "Sign the exact canonical bytes (sorted keys, no whitespace); a "
            "pretty-printed or key-ordered-as-received serialization will not "
            "verify.",
            f"The challenge expires after {proving.CHALLENGE_TTL_MINUTES} "
            "minutes — POST /prove again for a fresh one; only /prove/verify "
            "has effects.",
        ],
    }
    if agent:
        proof = agent.get("proof_of_conduct")
        if proof and proving._fresh(proof):
            state = f"proven — fresh until {proof['liveness_expires_at']}"
        elif proof:
            state = "proven but STALE — re-run the steps above to refresh liveness"
        else:
            state = "unproven — the steps above complete stage 1 → 2 on this visit"
        payload["your_status"] = {"agent_id": agent["id"],
                                  "proof_class": "credential_control" if custodial
                                  else "key_control",
                                  "state": state}
    return payload


# --------------------------------------------------------------------------
# A2A: agent card (discovery) + minimal JSON-RPC endpoint (message/send)
# --------------------------------------------------------------------------

def _swarm_skills(base: str) -> list[dict[str, Any]]:
    """One A2A skill per published, fixture-gated swarm capability, plus the
    generic invoke skill. Generated from the same registry as REST and MCP."""
    from .swarm.capabilities import CAPABILITIES
    skills: list[dict[str, Any]] = [{
        "id": "guild.invoke",
        "name": "Invoke a Guild utility capability (guest, free)",
        "description": (
            "Send 'invoke: <capability_id> <json payload>' to run one of the "
            "Guild's deterministic, fixture-verified utility capabilities "
            "(JSON repair/validate/diff, CSV↔JSON, date normalization, dedupe, "
            "record linking, regex extract, unit convert, semver, stats). No "
            "registration needed; rate-limited; every completion returns a "
            f"Guild-signed provenance envelope. Index of all capabilities with "
            f"schemas: {base}/.well-known/ag-identities/index.json — terms "
            f"(inspect before invoking): {base}/terms.json"),
        "tags": ["utility", "invocation", "deterministic", "guest"],
        "examples": ['invoke: json.repair {"text": "{\'a\': 1,}"}',
                     'invoke: text.date_normalize {"dates": ["3rd March 2026"]}'],
        "inputModes": ["text/plain"],
        "outputModes": ["application/json"],
    }]
    import json as _json
    for cap in sorted(CAPABILITIES.values(), key=lambda c: c.id):
        # Fully-formed, copy-pasteable example built from the capability's own
        # first fixture — cold-discovery testing showed generic clients template
        # their call off the example verbatim, and "{...}" placeholders made
        # them send un-runnable payloads.
        try:
            example_payload = _json.dumps(cap.fixtures[0]["input"])[:220]
        except Exception:
            example_payload = "{}"
        skills.append({
            "id": f"ag.{cap.id}",
            "name": cap.name,
            "description": cap.summary + " Send: 'invoke: " + cap.id + " <json>'.",
            "tags": list(cap.tags),
            "examples": [f"invoke: {cap.id} {example_payload}"],
            "inputModes": ["text/plain"],
            "outputModes": ["application/json"],
        })
    return skills


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
            *_swarm_skills(base),
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
    ua_tag = f"a2a:{real_ua}" if real_ua else "a2a/json-rpc"
    # Per-caller actor key (2026-07-08). Anonymous A2A callers used to collapse
    # into one literal "a2a" bucket, so a real decider and a polling monitor
    # were indistinguishable at actor level. Derive a stable, granular key from
    # the strongest identity signal the request carries (self-declared id →
    # token fingerprint → network+UA fingerprint → stable anon). Always
    # namespaced "a2a:" so it can never collide with a real billing key.
    client_host = request.client.host if request.client else ""
    actor = derive_a2a_actor(request.headers, client_host, text)

    # Infer the caller's intent BEFORE recording, so the caller's OWN event
    # carries the deciding signal (capability ask / prove question / advert)
    # instead of looking like a bare probe. Previously the primary query event
    # never carried the capability, so a genuine capability ask was
    # indistinguishable from a `ping` — which is why engagement could not be
    # read off the caller's own traffic.
    lowered = text.lower().strip()
    m = _CAP_RE.search(text)
    _adv_url = None
    _inv = re.match(r"^\s*invoke:\s*([a-z0-9_.\-]+)\s*(\{.*\})?\s*$", text, re.S | re.I)
    _inv_intent = bool(re.match(r"^\s*invoke\b", text, re.I))
    # A bare option-style reply: "1", "3", "user: 2", "(a)", "option 3" — a
    # machine selecting from a menu. Live telemetry (actor a2a:net:4580505b,
    # 2026-07-10) showed an LLM-driven client sending "user: 1" ×9 and
    # "user: 3" and dead-ending at probe_ack. The Guild never issues numbered
    # menus and /a2a is STATELESS (no conversation id, no continuation token,
    # no stored option set), so no numeric reply is ever resolvable here — the
    # honest machine answer is a structured clarification carrying the exact
    # explicit actions, not a generic ack.
    _opt = bool(re.match(
        r"^\s*(?:user:\s*)?(?:option\s*)?[\(\[]?(?:\d{1,3}|[a-e])[\)\].]?\s*$",
        text, re.I))
    # JSON skill invocation per our own agent card (2026-07-13). The card
    # advertises skill ids (guild.check / guild.capabilities / guild.invoke /
    # ag.<capability>) and an SDK-driven caller invokes them literally as
    # JSON — live telemetry caught {"skill":"guild.check","args":{}} falling
    # through to probe_ack. Card-advertised syntax must always resolve.
    _skill = _skill_call(text)
    _skill_payload: Optional[dict[str, Any]] = None
    if _skill is not None:
        _sid, _sargs = _skill
        _sid_l = _sid.lower()
        _cap_arg = _sargs.get("capability") or _sargs.get("cap")
        if _sid_l in ("guild.check", "check"):
            if isinstance(_cap_arg, str) and _cap_arg.strip():
                caller_kind, caller_cap = "capability_ask", _cap_arg.strip()
            else:
                caller_kind, caller_cap = "skill_args_missing", None
        elif _sid_l in ("guild.capabilities", "capabilities"):
            caller_kind, caller_cap = "capabilities_map", None
        elif _sid_l in ("guild.invoke", "invoke"):
            _cid = _sargs.get("capability_id") or _cap_arg
            if isinstance(_cid, str) and _cid.strip():
                caller_kind, caller_cap = "swarm_invoke_ask", _cid.strip()
                _sp = _sargs.get("payload")
                _skill_payload = _sp if isinstance(_sp, dict) else {
                    k: v for k, v in _sargs.items()
                    if k not in ("capability_id", "capability", "cap")}
            else:
                caller_kind, caller_cap = "swarm_invoke_malformed", None
        elif _sid_l.startswith("ag."):
            caller_kind, caller_cap = "swarm_invoke_ask", _sid_l[3:]
            _skill_payload = _sargs
        else:
            caller_kind, caller_cap = "skill_unknown", None
    elif _inv:
        caller_kind, caller_cap = "swarm_invoke_ask", _inv.group(1)
    elif _opt:
        caller_kind, caller_cap = "option_reply", None
    elif _inv_intent:
        # The caller clearly wants to invoke but the syntax is off (e.g. missing
        # capability id). A generic probe_ack here is a dead end for a machine —
        # answer with the exact corrective format instead.
        caller_kind, caller_cap = "swarm_invoke_malformed", None
    elif lowered in ("capabilities", "capability map", "supply", "demand"):
        caller_kind, caller_cap = "capabilities_map", None
    elif m:
        caller_kind, caller_cap = "capability_ask", m.group(1)
    elif _PROVE_INTENT_RE.search(text):
        caller_kind, caller_cap = "prove_howto", None
    elif (_adv_url := _advertised_url(text)):
        caller_kind, caller_cap = "endpoint_advert", None
    else:
        caller_kind, caller_cap = "probe", None

    # Keep a truncated copy of the inbound text: when an external agent makes
    # first contact we currently have no way to reach it back (Forge-9 taught
    # us this the hard way — registered with empty metadata, then went quiet).
    # The message body is the only artifact of the encounter; keep it.
    store.record_event(actor, "query", ua=ua_tag,
                       endpoint="a2a_message", text=text[:300],
                       caller_kind=caller_kind, capability=caller_cap)

    import json as _json
    if caller_kind == "swarm_invoke_ask":
        # A2A route into the acquisition gateway: same chokepoint, limits,
        # attribution, and signed provenance envelope as POST /invoke/{id}.
        from .swarm import gateway as _gw
        from .swarm.router import ensure_built as _ensure_built, _is_first_party as _fp
        _ensure_built()
        if _skill_payload is not None:
            _payload: Any = _skill_payload
        else:
            try:
                _payload = _json.loads(_inv.group(2)) if _inv.group(2) else {}
            except (_json.JSONDecodeError, ValueError):
                _payload = None
        if not isinstance(_payload, dict):
            payload = {"error": "send: invoke: <capability_id> <json object payload>",
                       "index": "/.well-known/ag-identities/index.json"}
        else:
            try:
                _status, payload = _gw.invoke(
                    store, caller_cap, _payload,
                    x_api_key=request.headers.get("x-api-key"),
                    client_host=client_host, ua=real_ua,
                    first_party=_fp(request.headers.get("x-guild-source")),
                    base=str(request.base_url).rstrip("/"))
            except _gw.Denied as _d:
                payload = {"denied": _d.kind, **_d.detail}
    elif caller_kind == "option_reply":
        # Machine-readable clarification for an unresolvable menu selection.
        payload = {
            "kind": "option_reply_without_context",
            "error": "no_session_context",
            "received": text[:40],
            "explanation": (
                "This A2A endpoint is stateless: there is no conversation id, "
                "no stored option set, and no continuation token, and the "
                "Guild never issues numbered menus — so a bare option reply "
                "like this cannot be resolved to an action. If a numbered "
                "list appeared in your context, it was composed on your side. "
                "Every Guild action is one self-contained message; pick one "
                "from `actions` and send it in full."),
            "actions": [
                {"action": "capabilities.map", "send": "capabilities",
                 "returns": "full supply/demand map"},
                {"action": "trust.check", "send": "check: <capability>",
                 "example": "check: fact-check",
                 "returns": "best-evidenced agent + verdict + reachability"},
                {"action": "capability.invoke",
                 "send": "invoke: <capability_id> <json payload>",
                 "example": 'invoke: json.repair {"text": "{\'a\': 1,}"}',
                 "schemas": "/.well-known/ag-identities/index.json",
                 "returns": "deterministic result + signed provenance"},
                {"action": "prove.howto", "send": "how do I prove key control?",
                 "returns": "the exact self-serve proving calls"},
                {"action": "register",
                 "send": None,
                 "http": {"method": "POST", "path": "/agents/register",
                          "body": {"name": "<you>", "capabilities": [],
                                   "metadata": {"endpoint": "<your URL>"}}},
                 "returns": "your agent_id + api key + guild_next"},
            ],
        }
    elif caller_kind == "swarm_invoke_malformed":
        from .swarm.capabilities import CAPABILITIES as _caps
        payload = {
            "error": "invoke_syntax",
            "expected": "invoke: <capability_id> <json object payload>",
            "example": 'invoke: json.repair {"text": "{\'a\': 1,}"}',
            "capability_ids": sorted(_caps.keys()),
            "schemas": "/.well-known/ag-identities/index.json",
            "terms": "/terms.json",
        }
    elif caller_kind == "capabilities_map":
        payload: dict[str, Any] = {
            "supplied": store.capability_index(),
            "demand": store.demand_summary(),
        }
    elif caller_kind == "skill_args_missing":
        # guild.check invoked per the card but without the capability arg
        # (exactly what a2a:net:8feb… sent 2026-07-13). The machine answer is
        # the exact corrected call, not a generic ack.
        payload = {
            "kind": "skill_args_missing",
            "error": "missing_capability",
            "skill": "guild.check",
            "expected": {"skill": "guild.check",
                         "args": {"capability": "<capability>"}},
            "example": {"skill": "guild.check",
                        "args": {"capability": "fact-check"}},
            "also_accepts": "plain text: 'check: <capability>'",
            "supplied_capabilities": store.capability_index(),
        }
    elif caller_kind == "skill_unknown":
        from .swarm.capabilities import CAPABILITIES as _caps
        payload = {
            "kind": "skill_not_found",
            "error": "unknown_skill",
            "received": (_skill[0] if _skill else "")[:80],
            "skills": {
                "guild.check": {"args": {"capability": "<capability>"}},
                "guild.capabilities": {"args": {}},
                "guild.invoke": {"args": {"capability_id": "<id>",
                                          "payload": {}}},
                **{f"ag.{cid}": "invoke with capability payload as args"
                   for cid in sorted(_caps.keys())},
            },
            "agent_card": "/.well-known/agent-card.json",
        }
    elif caller_kind == "capability_ask":
        payload = store.check(caller_cap)
    elif caller_kind == "prove_howto":
        # An agent asking how to prove gets the exact executable answer, not a
        # probe_ack. Recorded distinctly so surfaced→asked→completed is a
        # measurable funnel, not a guess.
        payload = _prove_instructions(text)
        _named = _AGENT_ID_RE.search(text)
        store.record_event(actor, "prove_howto_served", ua=ua_tag,
                           endpoint="a2a_message",
                           agent_id=(_named.group(0) if _named else None))
    elif _adv_url:
        # Adverts are endpoint declarations in disguise (IDEAS.md 2026-07-07,
        # the MetaVision lesson): a message carrying a third-party URL gets a
        # personalized declare/claim instruction, not a generic ack. Recorded
        # distinctly so advert → declared is a measurable funnel of its own.
        _adv_agent = _match_registered_agent(text)
        payload = _endpoint_declare_instructions(_adv_agent, _adv_url)
        store.record_event(actor, "endpoint_declare_howto_served", ua=ua_tag,
                           endpoint="a2a_message",
                           agent_id=(_adv_agent["id"] if _adv_agent else None),
                           advertised_url=_adv_url[:300])
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
            # First-contact dead-end fix (2026-07-07): live telemetry showed a
            # genuine external agent (a2a:python-httpx) pinging repeatedly and
            # never advancing. Every actionable path below (prove, endpoint)
            # requires an agent_id the anonymous prober does not yet have, so it
            # re-pinged instead of acting. A rational zero-state agent needs the
            # ONE call it can execute right now with no prior state and no schema
            # guessing. This is that call: a copy-pasteable register with a
            # concrete body. Its response returns your agent_id AND a guild_next
            # that carries the exact prove step — closing probe→register→prove.
            "register_now": {
                "why": ("You have no record here yet. This single call creates "
                        "one (free), returns your agent_id + a did:key you "
                        "control, and its guild_next tells you the exact next "
                        "call to earn your first verifiable evidence."),
                "method": "POST",
                "url": f"{proving.BASE}/agents/register",
                "headers": {"Content-Type": "application/json"},
                "example_body": {
                    "name": "your-agent-handle",
                    "capabilities": ["<what you can do, e.g. fact-check>"],
                    "metadata": {
                        "endpoint": "<your A2A or HTTP URL, so the Guild can "
                                    "reach you back with collaboration invites>"
                    },
                },
                "then": ("Read guild_next in the response, then POST to "
                         f"{proving.BASE}/agents/{{your_agent_id}}/prove to start the "
                         "free self-serve proving rung — no counterparty needed."),
            },
            # Indexer-aware self-description (2026-07-11). Live telemetry shows a
            # whole and growing CLASS of ecosystem-INTELLIGENCE crawlers probing
            # /a2a to characterise the Guild for indexes that OTHER agents then
            # query for discovery: DEMOS-Organism/1.0 (the-organism.xyz, indexes
            # 1344 agents), AgentsCensusBot, Chiark (agent quality index),
            # AgenstryBot, A2A-Registry-TaskProbe — 5+ distinct in a single
            # 200-event window. register_now answers the wrong job for an
            # indexer: it does not want to be hired or to register, it wants a
            # crisp, ingestible record. Giving it an accurate canonical
            # self-description serves the crawler's OWN task and spreads correct
            # Guild representation across every downstream discovery surface at
            # zero cost. Only real, live fields — no marketing claims.
            "self_description": {
                "note": ("Machine-readable record for ecosystem indexes and "
                         "directory crawlers: this is how to represent Agent "
                         "Guild in your catalog."),
                "name": "Agent Guild",
                "category": "trust-and-settlement-middleware",
                "summary": ("Registry-backed trust middleware for agent-to-agent "
                            "coordination. Before one agent delegates to another, "
                            "the Guild returns a hire/caution/avoid verdict from "
                            "evidence-backed work attestations (seed-anchored "
                            "EigenTrust + structural collusion/Sybil detection), "
                            "so manufactured praise does not move scores."),
                "capabilities_supplied": len(store.capability_index()),
                "protocols": ["A2A", "MCP", "REST"],
                "standard": "AGI-1 (open, vendor-neutral trust-passport format)",
                "identity": ("agents identified by W3C did:key (Ed25519); trust "
                             "passports are W3C Verifiable Credentials verifiable "
                             "offline against the issuer's did:key"),
                "urls": {
                    "home": proving.BASE,
                    "agent_card": f"{proving.BASE}/.well-known/agent-card.json",
                    "mcp": f"{proving.BASE}/mcp/",
                    "trust_check": f"{proving.BASE}/check?capability=<capability>",
                    "standard": f"{proving.BASE}/standard",
                    "for_agents": f"{proving.BASE}/for-agents",
                },
            },
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
            "auth": ("custodial agents (Guild-issued api_key) send X-API-Key "
                     "on both calls; self-sovereign agents (own public_key) "
                     "need no header — the signature is the proof"),
            "ask_me": ("send a message containing 'prove' (include your "
                       "agent_id for personalized steps) and this endpoint "
                       "replies with the exact calls, payloads, and signing "
                       "instructions"),
            "via_mcp": "MCP tools guild_prove / guild_prove_verify at /mcp",
        },
    }
    store.record_event(actor, "prove_surfaced", ua=ua_tag,
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
