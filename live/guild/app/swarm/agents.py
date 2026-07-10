"""L4 — Discovery Agents: a small population (Pilot A: 5) of bounded-mandate
agents that keep AG discoverable and measure how machines interpret it.

Not marketing bots: they only touch documented machine interfaces, read-only
where external, through ONE policy chokepoint that enforces — in order —
kill switch, per-agent daily action budget, target allowlist, then logs a
reason-coded action record. They have no shell, no deploy access, and no
store-write access beyond their own action ledger + adapter health.

Ticks are operator-triggered (admin POST /swarm/agents/run or the scheduled
ops task) — nothing runs autonomously in the background in Pilot A."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx

from . import mapper
from .identity import registry

DAILY_ACTION_BUDGET = 40          # hard cap per agent per UTC day
ALLOWED_URL_PREFIXES = (          # external allowlist — https only, public registries
    "https://registry.modelcontextprotocol.io/",
    "https://glama.ai/",
    "https://smithery.ai/",
)
MAX_ACTIONS_KEPT = 2000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_action(store, *, agent: str, reason_code: str, target: str,
                protocol: str, outcome: str, policy: str,
                request_meta: Optional[dict] = None,
                response_meta: Optional[dict] = None,
                cost: float = 0.0, retry_state: str = "none") -> dict:
    rec = {"at": _now(), "agent": agent, "reason_code": reason_code,
           "target": target, "protocol": protocol,
           "request": request_meta or {}, "response": response_meta or {},
           "cost": cost, "outcome": outcome, "attribution": "ag_internal",
           "policy_decision": policy, "retry_state": retry_state}
    with store.lock:
        actions = store.swarm_state.setdefault("actions", [])
        actions.append(rec)
        if len(actions) > MAX_ACTIONS_KEPT:
            del actions[: len(actions) - MAX_ACTIONS_KEPT]
    return rec


def _budget_left(store, agent: str) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    used = sum(1 for a in store.swarm_state.get("actions", [])
               if a["agent"] == agent and a["at"][:10] == today
               and a["policy_decision"] == "allowed")
    return DAILY_ACTION_BUDGET - used


def _killed(store) -> bool:
    from .gateway import swarm_killed
    return swarm_killed(store)


def _guarded_fetch(store, agent: str, reason_code: str, url: str,
                   client: httpx.Client) -> Optional[httpx.Response]:
    """The single chokepoint for every EXTERNAL action."""
    if _killed(store):
        _log_action(store, agent=agent, reason_code=reason_code, target=url,
                    protocol="https", outcome="blocked", policy="kill_switch")
        return None
    if _budget_left(store, agent) <= 0:
        _log_action(store, agent=agent, reason_code=reason_code, target=url,
                    protocol="https", outcome="blocked", policy="budget_exhausted")
        return None
    if not any(url.startswith(p) for p in ALLOWED_URL_PREFIXES):
        _log_action(store, agent=agent, reason_code=reason_code, target=url,
                    protocol="https", outcome="blocked", policy="not_allowlisted")
        return None
    # circuit breaker: 3 consecutive failures against a host pauses it for the day
    host = url.split("/", 3)[2]
    breaker = store.swarm_state.setdefault("breakers", {})
    b = breaker.get(host, {"failures": 0, "open_date": None})
    if b.get("open_date") == datetime.now(timezone.utc).date().isoformat():
        _log_action(store, agent=agent, reason_code=reason_code, target=url,
                    protocol="https", outcome="blocked", policy="circuit_open")
        return None
    try:
        resp = client.get(url, timeout=10.0, headers={
            "User-Agent": "agent-guild-discovery/1.0 (verification; "
                          "https://agent-guild-5d5r.onrender.com)"})
        b["failures"] = 0
        breaker[host] = b
        _log_action(store, agent=agent, reason_code=reason_code, target=url,
                    protocol="https", outcome=f"http_{resp.status_code}",
                    policy="allowed",
                    response_meta={"status": resp.status_code,
                                   "bytes": len(resp.content)})
        return resp
    except httpx.HTTPError as e:
        b["failures"] = b.get("failures", 0) + 1
        if b["failures"] >= 3:
            b["open_date"] = datetime.now(timezone.utc).date().isoformat()
        breaker[host] = b
        _log_action(store, agent=agent, reason_code=reason_code, target=url,
                    protocol="https", outcome=f"error:{type(e).__name__}",
                    policy="allowed", retry_state="breaker_counted")
        return None


# --------------------------------------------------------------------------
# the five Pilot A mandates
# --------------------------------------------------------------------------

def _tick_verifier(store, asgi_client, http_client) -> dict:
    """Mandate: confirm AG's own machine surfaces + external listings remain
    fetchable and valid. Own-domain checks run IN-PROCESS (asgi_client) so a
    single-worker deployment can never deadlock on itself."""
    results = {}
    for path, marker in mapper.SELF_SURFACES.items():
        if _killed(store):
            break
        try:
            r = asgi_client.get(path)
            ok = r.status_code == 200 and (not marker or marker in r.text)
            results[path] = "ok" if ok else f"unexpected:{r.status_code}"
        except Exception as e:  # noqa: BLE001
            results[path] = f"error:{type(e).__name__}"
        _log_action(store, agent="verifier", reason_code="self_surface_check",
                    target=path, protocol="asgi", outcome=results[path],
                    policy="allowed")
    for eco in mapper.ECOSYSTEMS:
        for url in eco["verify_urls"]:
            resp = _guarded_fetch(store, "verifier", "registry_listing_check",
                                  url, http_client)
            status = ("ok" if resp is not None and resp.status_code == 200
                      else "failed" if resp is not None else "blocked_or_error")
            mapper.note_adapter_health(store, eco["id"], status, url)
            results[url] = status
    return {"agent": "verifier", "results": results}


def _tick_publisher(store, asgi_client, http_client) -> dict:
    """Mandate: detect drift between the capability registry and what is
    published; prepare (never send) submissions for needs_human ecosystems."""
    idx = registry.index("")
    published = {e["capability"] for e in idx["identities"]}
    from .capabilities import CAPABILITIES
    drift = sorted(set(CAPABILITIES) - published)
    needs_human = [e["id"] for e in mapper.ECOSYSTEMS
                   if "needs_human" in (e.get("terms") or "")]
    _log_action(store, agent="publisher", reason_code="drift_check",
                target="identity_index", protocol="internal",
                outcome=f"unpublished:{len(drift)}", policy="allowed",
                response_meta={"unpublished": drift,
                               "needs_human_targets": needs_human})
    return {"agent": "publisher", "unpublished_capabilities": drift,
            "needs_human_targets": needs_human,
            "note": "submissions for needs_human targets are draft-only "
                    "(live/outreach); a human sends them"}


def _tick_gap_scout(store, asgi_client, http_client) -> dict:
    """Mandate: read unmet demand from our own surfaces and propose (only
    propose) seed-capability candidates."""
    demand = store.demand_summary() if hasattr(store, "demand_summary") else {}
    asks = [e.get("capability") for e in store.events
            if e.get("type") == "query" and e.get("caller_kind") == "capability_ask"
            and e.get("capability")]
    from .capabilities import CAPABILITIES
    supplied = set(CAPABILITIES)
    proposals = sorted({a for a in asks if a and a not in supplied})[:20]
    _log_action(store, agent="gap-scout", reason_code="demand_scan",
                target="events+demand_watches", protocol="internal",
                outcome=f"proposals:{len(proposals)}", policy="allowed",
                response_meta={"proposals": proposals})
    return {"agent": "gap-scout", "unmet_demand": demand,
            "capability_proposals": proposals,
            "note": "proposals only — capabilities enter via template + fixtures"}


def _tick_interop_tester(store, asgi_client, http_client) -> dict:
    """Mandate: replay a standard external client's first-contact sequence
    in-process and score it (card → index → identity → schema-valid invoke)."""
    steps = {}
    try:
        card = asgi_client.get("/.well-known/agent-card.json").json()
        steps["agent_card"] = "ok" if card.get("skills") else "no_skills"
        idx = asgi_client.get("/.well-known/ag-identities/index.json").json()
        steps["identity_index"] = ("ok" if idx.get("count", 0) > 0
                                   else "empty_index")
        first = idx["identities"][0]
        doc = asgi_client.get(f"/identities/{first['ag_id']}").json()
        steps["identity_doc"] = ("ok" if doc.get("signature", {}).get("signature")
                                 else "unsigned")
        r = asgi_client.post("/invoke/json.canonicalize",
                             json={"value": {"interop": True}},
                             headers={"X-Guild-Source": os.environ.get(
                                 "GUILD_FIRST_PARTY_TOKEN", "swarm-interop")})
        body = r.json()
        steps["invoke"] = ("ok" if r.status_code == 200 and body.get("ok")
                           else f"failed:{r.status_code}")
        steps["provenance_signed"] = ("ok" if body.get("provenance", {})
                                      .get("verification", {}).get("signature")
                                      else "missing")
    except Exception as e:  # noqa: BLE001
        steps["error"] = f"{type(e).__name__}: {e}"[:200]
    outcome = "ok" if all(v == "ok" for v in steps.values()) else "degraded"
    _log_action(store, agent="interop-tester", reason_code="external_replay",
                target="card→index→identity→invoke", protocol="asgi",
                outcome=outcome, policy="allowed", response_meta=steps)
    return {"agent": "interop-tester", "outcome": outcome, "steps": steps}


def _tick_wording_optimizer(store, asgi_client, http_client) -> dict:
    """Mandate: measure which machine-readable signals lead to invocation —
    data only; description changes ship via normal versioned review."""
    counters = store.swarm_state.get("counters", {})
    fetches: dict[str, int] = {}
    for e in store.events:
        if e.get("type") == "swarm_identity_fetch" and e.get("capability"):
            fetches[e["capability"]] = fetches.get(e["capability"], 0) + 1
    report = []
    from .capabilities import CAPABILITIES
    for cap_id in sorted(CAPABILITIES):
        inv = counters.get(cap_id, {}).get("invocations", 0)
        f = fetches.get(cap_id, 0)
        report.append({"capability": cap_id, "identity_fetches": f,
                       "invocations": inv,
                       "fetch_to_invoke": round(inv / f, 3) if f else None})
    _log_action(store, agent="wording-optimizer", reason_code="signal_analysis",
                target="counters+events", protocol="internal",
                outcome=f"capabilities:{len(report)}", policy="allowed")
    return {"agent": "wording-optimizer", "conversion": report,
            "note": "wording changes are proposals for versioned review, "
                    "never auto-applied"}


MANDATES: dict[str, Callable] = {
    "verifier": _tick_verifier,
    "publisher": _tick_publisher,
    "gap-scout": _tick_gap_scout,
    "interop-tester": _tick_interop_tester,
    "wording-optimizer": _tick_wording_optimizer,
}


def run_tick(store, asgi_client, agents: Optional[list[str]] = None) -> dict:
    """Run one tick of selected (default: all) discovery agents."""
    if _killed(store):
        return {"ran": [], "blocked": "kill_switch"}
    out = {}
    with httpx.Client(follow_redirects=True) as http_client:
        for name in (agents or list(MANDATES)):
            fn = MANDATES.get(name)
            if fn is None:
                out[name] = {"error": "unknown agent"}
                continue
            try:
                out[name] = fn(store, asgi_client, http_client)
            except Exception as e:  # noqa: BLE001 — one agent's crash never stops the tick
                out[name] = {"error": f"{type(e).__name__}: {e}"[:300]}
                _log_action(store, agent=name, reason_code="tick",
                            target="self", protocol="internal",
                            outcome=f"crash:{type(e).__name__}", policy="allowed")
    with store.lock:
        store._save()
    return {"ran": list(out), "results": out}


def agent_status(store) -> dict:
    actions = store.swarm_state.get("actions", [])
    return {
        "agents": [{"name": n, "mandate": (fn.__doc__ or "").strip().split("\n")[0],
                    "daily_action_budget": DAILY_ACTION_BUDGET,
                    "budget_left_today": _budget_left(store, n)}
                   for n, fn in MANDATES.items()],
        "allowlist": list(ALLOWED_URL_PREFIXES),
        "recent_actions": actions[-50:],
        "action_count": len(actions),
    }
