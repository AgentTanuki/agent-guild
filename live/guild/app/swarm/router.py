"""Discovery Swarm — public REST surface + operator dashboard.

Endpoints (all machine-first):
  GET  /.well-known/ag-identities/index.json   identity index (L1)
  GET  /identities/{ag_id}                     signed identity document (L1)
  GET  /terms.json                             terms BEFORE invocation (L5)
  POST /invoke/{capability_id}                 guest/member invocation (L5)
  GET  /swarm/capabilities                     registry + schemas + gate results
  GET  /swarm/match                            utility-ranked capability match
  GET  /swarm/ecosystems                       machine ecosystem map (L3)
  GET  /swarm/stats                            machine-growth metrics (L6)
  GET  /swarm/graph                            discovery/referral graph (L6)
  GET  /swarm/agents                           discovery-agent status (L4)
  POST /swarm/agents/run        [admin]        run a discovery-agent tick (L4)
  POST /swarm/kill | /swarm/revive [admin]     global kill switch
  GET  /dashboard                              machine-growth dashboard (Ross)
"""
from __future__ import annotations

import html
import os
from typing import Any, Optional

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..state import store
from .. import journey as journey_engine
from . import agents as swarm_agents
from . import gateway, graph, mapper, utility
from .capabilities import CAPABILITIES
from .identity import registry, SWARM_TAG

router = APIRouter()

BASE = journey_engine.BASE  # canonical public origin — identity docs cite it


from .. import firstparty as _fp_auth


def _is_first_party(x_guild_source: Optional[str],
                    x_first_party: Optional[str] = None) -> bool:
    """Constant-time first-party check (dedicated + legacy header). See
    app/firstparty.py."""
    return _fp_auth.is_first_party(x_first_party, x_guild_source)


def _fp_from_request(request) -> tuple[bool, str]:
    """(is_first_party, role) from a request's first-party headers — the
    dedicated X-Agent-Guild-First-Party (preferred), the legacy X-Guild-Source,
    and the optional X-Agent-Guild-Role (test|internal, default internal)."""
    h = request.headers
    ok = _fp_auth.is_first_party(h.get(_fp_auth.HEADER), h.get(_fp_auth.LEGACY_HEADER))
    return ok, _fp_auth.role_of(h.get(_fp_auth.ROLE_HEADER))


def _stamp_fp(request) -> None:
    ok, role = _fp_from_request(request)
    if ok and store.events:
        store.events[-1]["fp"] = True
        store.events[-1]["fp_role"] = role


def _require_admin(x_admin_token: Optional[str]) -> None:
    token = os.environ.get("GUILD_ADMIN_TOKEN", "")
    if token and x_admin_token != token:
        raise HTTPException(401, "invalid X-Admin-Token")


def ensure_built() -> None:
    """Publish gate + identity build + idempotent supply registration.
    Safe to call on every request; does work only once per process."""
    if registry.built:
        return
    gi = store.guild_identity()
    # register each capability as a first-party guild agent (supply-side
    # listing), idempotently by capability id.
    owner_ids: dict[str, str] = {}
    with store.lock:
        existing = {(a.get("metadata") or {}).get("swarm_capability"): a["id"]
                    for a in store.agents.values()
                    if (a.get("metadata") or {}).get(SWARM_TAG)}
    for cap_id, cap in CAPABILITIES.items():
        if cap_id in existing:
            owner_ids[cap_id] = existing[cap_id]
            continue
        rec = store.register_agent(
            name=f"AG {cap.name}", capabilities=[cap_id],
            metadata={SWARM_TAG: True, "swarm_capability": cap_id,
                      "endpoint": f"{BASE}/invoke/{cap_id}",
                      "est_latency_ms": cap.est_latency_ms,
                      "price_per_call": 0},
            first_party=True)
        owner_ids[cap_id] = rec["id"]
    registry.build(BASE, gi, owner_ids)


# --------------------------------------------------------------------------
# L1 — identity surfaces
# --------------------------------------------------------------------------

@router.get("/.well-known/ag-identities/index.json")
def identity_index(request: Request,
                   x_guild_source: Optional[str] = Header(None)):
    ensure_built()
    with store.lock:
        store.record_event(None, "swarm_index_fetch",
                           ua=request.headers.get("user-agent", ""))
        _stamp_fp(request)
    return registry.index(BASE)


@router.get("/identities/{ag_id}")
def identity_document(ag_id: str, request: Request,
                      x_guild_source: Optional[str] = Header(None)):
    ensure_built()
    doc = registry.get(ag_id)
    if doc is None:
        raise HTTPException(404, {"error": "unknown identity",
                                  "index": f"{BASE}/.well-known/ag-identities/index.json"})
    with store.lock:
        store.record_event(None, "swarm_identity_fetch",
                           ua=request.headers.get("user-agent", ""),
                           capability=doc["identity"]["capability"]["id"])
        _stamp_fp(request)
    return doc


@router.get("/terms.json")
def terms(request: Request, x_guild_source: Optional[str] = Header(None)):
    with store.lock:
        store.record_event(None, "swarm_terms_fetch",
                           ua=request.headers.get("user-agent", ""))
        _stamp_fp(request)
    return gateway.terms(BASE)


# --------------------------------------------------------------------------
# L5 — invocation gateway
# --------------------------------------------------------------------------

@router.post("/invoke/{capability_id}")
def invoke(capability_id: str, request: Request,
           payload: Any = Body(...),
           x_api_key: Optional[str] = Header(None),
           x_guild_source: Optional[str] = Header(None)):
    ensure_built()
    try:
        status, body = gateway.invoke(
            store, capability_id, payload,
            x_api_key=x_api_key,
            client_host=(request.client.host if request.client else ""),
            ua=request.headers.get("user-agent", ""),
            first_party=_fp_from_request(request)[0],
            first_party_role=_fp_from_request(request)[1],
            base=BASE)
    except gateway.Denied as d:
        with store.lock:
            store.record_event(None, "swarm_invoke_denied",
                               ua=request.headers.get("user-agent", ""),
                               capability=capability_id, reason=d.kind)
            _stamp_fp(request)
        headers = {}
        if d.status == 429:
            headers["Retry-After"] = str(d.detail.get("retry_after_seconds", 60))
        return JSONResponse(d.detail | {"denied": d.kind,
                                        "terms": f"{BASE}/terms.json"},
                            status_code=d.status, headers=headers)
    return JSONResponse(body, status_code=status)


# --------------------------------------------------------------------------
# registry / matching / map / metrics
# --------------------------------------------------------------------------

@router.get("/swarm/capabilities")
def swarm_capabilities():
    ensure_built()
    caps = []
    for cap_id, cap in sorted(CAPABILITIES.items()):
        doc = registry.for_capability(cap_id)
        caps.append({
            "id": cap.id, "version": cap.version, "summary": cap.summary,
            "tags": list(cap.tags), "input_schema": cap.input_schema,
            "output_schema": cap.output_schema,
            "failure_modes": list(cap.failure_modes),
            "prohibited_uses": list(cap.prohibited_uses),
            "safety_class": cap.safety_class,
            "demand_hypothesis": cap.demand_hypothesis,
            "baseline": cap.baseline,
            "published": doc is not None,
            "ag_id": doc["identity"]["ag_id"] if doc else None,
            "gate": registry.gate_results().get(cap_id),
        })
    return {"count": len(caps), "capabilities": caps,
            "publish_gate": "an identity is only published after its fixture "
                            "suite passes fully"}


@router.get("/swarm/match")
def swarm_match(task: str = Query(..., max_length=500),
                limit: int = Query(5, ge=1, le=16)):
    ensure_built()
    return utility.match(store, task, limit=limit)


@router.get("/swarm/ecosystems")
def swarm_ecosystems():
    return mapper.ecosystem_map(store)


@router.get("/swarm/stats")
def swarm_stats():
    ensure_built()
    return {
        "growth": graph.growth_stats(store),
        "capability_counters": store.swarm_state.get("counters", {}),
        "publish_gate": {k: {kk: v[kk] for kk in ("ok", "passed", "total")}
                         for k, v in registry.gate_results().items()},
        "kill_switch": {"active": gateway.swarm_killed(store)},
        "limits": {"guest_daily": gateway.GUEST_DAILY_LIMIT,
                   "member_daily": gateway.MEMBER_DAILY_LIMIT,
                   "global_per_minute": gateway.GLOBAL_PER_MINUTE,
                   "max_payload_bytes": gateway.MAX_PAYLOAD_BYTES},
    }


@router.get("/swarm/graph")
def swarm_graph():
    return graph.build_graph(store)


# --------------------------------------------------------------------------
# L4 — discovery agents + kill switch (admin)
# --------------------------------------------------------------------------

@router.get("/swarm/agents")
def swarm_agent_status():
    return swarm_agents.agent_status(store)


@router.post("/swarm/agents/run")
def swarm_agents_run(x_admin_token: Optional[str] = Header(None),
                     agents: Optional[list[str]] = Body(None, embed=True)):
    _require_admin(x_admin_token)
    ensure_built()
    from fastapi.testclient import TestClient
    from ..main import app  # late import: main is fully loaded at request time
    # NOTE: no context manager — entering it would re-run the app lifespan
    # (bootstrap + the MCP session manager, which must not start twice).
    client = TestClient(app, headers={"X-Guild-Source": os.environ.get(
        "GUILD_FIRST_PARTY_TOKEN", "swarm-ops")})
    return swarm_agents.run_tick(store, client, agents)


@router.post("/swarm/kill")
def swarm_kill(x_admin_token: Optional[str] = Header(None),
               reason: str = Body("operator kill", embed=True)):
    _require_admin(x_admin_token)
    gateway.set_killed(store, True, reason)
    return {"killed": True, "reason": reason}


@router.post("/swarm/revive")
def swarm_revive(x_admin_token: Optional[str] = Header(None)):
    _require_admin(x_admin_token)
    gateway.set_killed(store, False)
    return {"killed": False}


# --------------------------------------------------------------------------
# Ross's machine-growth dashboard (human-readable; machine activity only)
# --------------------------------------------------------------------------

_DASH_CSS = """
:root{color-scheme:dark}body{margin:0;background:#0b0e14;color:#e6e9ef;
font:14px/1.5 -apple-system,system-ui,sans-serif;padding:28px;max-width:1100px;margin:auto}
h1{font-size:20px;margin:0 0 2px}h2{font-size:15px;margin:26px 0 8px;color:#9aa3b5}
.sub{color:#8a93a6;margin:0 0 18px;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}
.card{background:#11151f;border:1px solid #28303f;border-radius:10px;padding:12px 14px}
.card .v{font-size:22px;font-weight:600}.card .k{color:#8a93a6;font-size:12px}
table{border-collapse:collapse;width:100%;font-size:13px}
td,th{border-bottom:1px solid #1d2432;padding:6px 8px;text-align:left}
th{color:#8a93a6;font-weight:500}.ok{color:#34d399}.bad{color:#f87171}
.warn{color:#fbbf24}.muted{color:#5b6373}code{background:#11151f;border:1px solid
#28303f;border-radius:5px;padding:1px 5px;font-size:12px}
"""


def _n(x) -> str:
    return html.escape(str(x))


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    ensure_built()
    g = graph.growth_stats(store)
    ge, fp = g["genuine_external"], g["ag_internal_first_party"]
    un = g["unattributable_external"]
    counters = store.swarm_state.get("counters", {})
    gate = registry.gate_results()
    inst = store.instrumentation()
    dg = graph.build_graph(store)
    organic_actors = [a for a in dg["actors"] if a.get("organic")]
    eco = mapper.ecosystem_map(store)["ecosystems"]
    actions = store.swarm_state.get("actions", [])[-15:]
    killed = gateway.swarm_killed(store)

    cards = [
        ("Published identities", sum(1 for r in gate.values() if r["ok"])),
        ("External discovery fetches", ge["discovery_fetches"]),
        ("External first invocations", ge["first_invocations"]),
        ("External completions", ge["successful_completions"]),
        ("External repeat callers", ge["repeat_callers"]),
        ("Registrations via referral", g["machine_registrations_via_referral"]),
        ("% acquired autonomously", g["pct_members_acquired_autonomously"] or 0),
        ("Genuine-external actors (all surfaces)",
         len(inst.get("genuine_external_actors", []))),
    ]
    rows_caps = ""
    for cap_id in sorted(CAPABILITIES):
        c = counters.get(cap_id, {})
        r = gate.get(cap_id, {})
        inv = c.get("invocations", 0)
        ok_rate = (f"{100 * c.get('successes', 0) / inv:.0f}%" if inv else "—")
        avg = (f"{c.get('total_latency_ms', 0) / inv:.1f}ms" if inv else "—")
        health = ("<span class=ok>passing</span>" if r.get("ok")
                  else "<span class=bad>FAILING — unpublished</span>")
        rows_caps += (f"<tr><td><code>{_n(cap_id)}</code></td><td>{health}</td>"
                      f"<td>{inv}</td><td>{ok_rate}</td><td>{avg}</td></tr>")
    rows_eco = "".join(
        f"<tr><td>{_n(e['name'])}</td><td>{_n(e['protocol'])}</td>"
        f"<td>{_n(e['ag_coverage'])}</td>"
        f"<td class={'ok' if e['adapter_health'] == 'ok' else 'muted'}>"
        f"{_n(e['adapter_health'])}</td><td class=muted>{_n(e['last_verified'] or '—')}</td></tr>"
        for e in eco)
    rows_actors = "".join(
        f"<tr><td><code>{_n(a['actor'][:40])}</code></td><td>{_n(a['class'])}</td>"
        f"<td>{a['discoveries']}</td><td>{a['invocations']}</td>"
        f"<td>{_n(', '.join(a['capabilities'][:4]))}</td>"
        f"<td class=muted>{_n((a['last_seen'] or '')[:19])}</td></tr>"
        for a in dg["actors"][:20]) or \
        "<tr><td colspan=6 class=muted>no swarm traffic yet</td></tr>"
    rows_actions = "".join(
        f"<tr><td>{_n(a['agent'])}</td><td>{_n(a['reason_code'])}</td>"
        f"<td class=muted>{_n(str(a['target'])[:60])}</td><td>{_n(a['outcome'])}</td>"
        f"<td>{_n(a['policy_decision'])}</td><td class=muted>{_n(a['at'][:19])}</td></tr>"
        for a in reversed(actions)) or \
        "<tr><td colspan=6 class=muted>no discovery-agent actions yet — POST /swarm/agents/run</td></tr>"

    kill_banner = ("<div class='card' style='border-color:#f87171'><span class=bad>"
                   "KILL SWITCH ACTIVE</span> — all swarm invocations disabled</div>"
                   if killed else "")
    page = f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>AG Machine Growth</title><style>{_DASH_CSS}</style></head><body>
<h1>Agent Guild — Machine Growth</h1>
<p class=sub>Machine activity only. Headline numbers are GENUINE EXTERNAL
(attribution-verified); AG-internal and unattributable traffic shown separately,
never merged. JSON: <code>/swarm/stats</code> · graph: <code>/swarm/graph</code></p>
{kill_banner}
<div class=grid>{''.join(f"<div class=card><div class=v>{_n(v)}</div><div class=k>{_n(k)}</div></div>" for k, v in cards)}</div>
<h2>External vs internal funnel (swarm surfaces)</h2>
<table><tr><th></th><th>discovery fetches</th><th>first invocations</th>
<th>total invocations</th><th>completions</th><th>repeat callers</th></tr>
<tr><td class=ok>genuine external</td><td>{ge['discovery_fetches']}</td>
<td>{ge['first_invocations']}</td><td>{ge['total_invocations']}</td>
<td>{ge['successful_completions']}</td><td>{ge['repeat_callers']}</td></tr>
<tr><td class=warn>unattributable external</td><td>{un['discovery_fetches']}</td>
<td>{un['first_invocations']}</td><td>{un['total_invocations']}</td>
<td>{un['successful_completions']}</td><td>{un['repeat_callers']}</td></tr>
<tr><td class=muted>AG-internal (excluded from growth)</td><td>{fp['discovery_fetches']}</td>
<td>{fp['first_invocations']}</td><td>{fp['total_invocations']}</td>
<td>{fp['successful_completions']}</td><td>{fp['repeat_callers']}</td></tr></table>
<h2>Identities ({len(CAPABILITIES)}) — health, demand, dormancy</h2>
<table><tr><th>capability</th><th>gate</th><th>invocations</th>
<th>success</th><th>avg latency</th></tr>{rows_caps}</table>
<h2>Ecosystem coverage</h2>
<table><tr><th>ecosystem</th><th>protocol</th><th>AG coverage</th>
<th>adapter</th><th>last verified</th></tr>{rows_eco}</table>
<h2>Recent actors (swarm surfaces)</h2>
<table><tr><th>actor</th><th>class</th><th>discoveries</th><th>invocations</th>
<th>capabilities</th><th>last seen</th></tr>{rows_actors}</table>
<h2>Discovery-agent action ledger (last 15)</h2>
<table><tr><th>agent</th><th>reason</th><th>target</th><th>outcome</th>
<th>policy</th><th>at</th></tr>{rows_actions}</table>
<h2>Security / abuse</h2>
<table><tr><th>signal</th><th>value</th></tr>
<tr><td>denied invocations (rate/size/kill)</td>
<td>{sum(1 for e in store.events if e.get('type') == 'swarm_invoke_denied')}</td></tr>
<tr><td>organic actors on swarm surfaces</td><td>{len(organic_actors)}</td></tr>
<tr><td>kill switch</td><td>{'ACTIVE' if killed else 'off'}</td></tr></table>
<p class=sub style="margin-top:22px">Experience records:
{len(store.swarm_state.get('experience', []))} (observational-learning substrate,
shape-stats only) · Costs: compute-only, ~$0 marginal · hard caps:
guest {gateway.GUEST_DAILY_LIMIT}/day, member {gateway.MEMBER_DAILY_LIMIT}/day,
global {gateway.GLOBAL_PER_MINUTE}/min</p>
</body></html>"""
    return HTMLResponse(page)
