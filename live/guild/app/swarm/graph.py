"""L6 — discovery & referral graph: who discovered what, what they invoked,
and whether it led to registration. Organic vs AG-internal is decided by the
existing attribution layer (single source of truth) — AG-owned synthetic
interactions are excluded from every growth number by construction."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..attribution import attribution_class, is_genuine_external

DISCOVERY_EVENT_TYPES = {"swarm_index_fetch", "swarm_identity_fetch",
                         "swarm_terms_fetch"}
INVOKE_EVENT_TYPES = {"swarm_invoke"}


def _swarm_events(store) -> list[dict]:
    return [e for e in store.events
            if e.get("type") in DISCOVERY_EVENT_TYPES | INVOKE_EVENT_TYPES]


def referral_bindings(store) -> list[dict]:
    """Registrations that carried a swarm referral token in their metadata —
    the machine-to-machine acquisition edge (token issued at invocation time,
    presented at registration time; nothing is paid automatically)."""
    tokens = store.swarm_state.get("referral_tokens", {})
    out = []
    for agent in store.agents.values():
        tok = (agent.get("metadata") or {}).get("referral_token")
        if tok and tok in tokens:
            t = tokens[tok]
            out.append({"agent_id": agent["id"],
                        "registered_at": agent.get("created_at"),
                        "first_party": bool(agent.get("first_party")),
                        "via_capability": t["capability"],
                        "invocation_id": t["invocation_id"],
                        "token_issued_at": t["issued_at"]})
    return out


def build_graph(store) -> dict:
    """Actor-level discovery→invoke→register paths, labelled organic vs internal."""
    actors: dict[str, dict] = {}
    for e in _swarm_events(store):
        key = e.get("actor") or e.get("key") or "anon"
        a = actors.setdefault(key, {
            "actor": key, "class": attribution_class(e),
            "discoveries": 0, "invocations": 0, "successes": 0,
            "capabilities": set(), "first_seen": e.get("at"),
            "last_seen": e.get("at")})
        a["last_seen"] = e.get("at")
        if e["type"] in DISCOVERY_EVENT_TYPES:
            a["discoveries"] += 1
        else:
            a["invocations"] += 1
            if e.get("outcome") == "success":
                a["successes"] += 1
        if e.get("capability"):
            a["capabilities"].add(e["capability"])
    nodes = []
    for a in actors.values():
        a["capabilities"] = sorted(a["capabilities"])
        a["organic"] = a["class"] == "genuine_external"
        nodes.append(a)
    bindings = referral_bindings(store)
    return {
        "schema_version": "ag-discovery-graph/1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "actors": sorted(nodes, key=lambda x: x["last_seen"] or "", reverse=True),
        "registrations_via_referral": bindings,
        "organic_registrations_via_referral": [
            b for b in bindings if not b["first_party"]],
        "note": "actors with class first_party/tooling_or_ours are AG-internal "
                "or unattributable and are excluded from growth metrics",
    }


def growth_stats(store) -> dict:
    """The primary-metric rollup. External-only headline; internal side-by-side
    for honesty, never merged."""
    ev = _swarm_events(store)
    ext = [e for e in ev if is_genuine_external(e)]
    fp = [e for e in ev if e.get("fp")]
    other = [e for e in ev if not e.get("fp") and not is_genuine_external(e)]

    def funnel(events: list[dict]) -> dict:
        invokes = [e for e in events if e["type"] in INVOKE_EVENT_TYPES]
        actors_inv = {}
        for e in invokes:
            actors_inv.setdefault(e.get("actor") or e.get("key") or "anon",
                                  []).append(e)
        return {
            "discovery_fetches": sum(1 for e in events
                                     if e["type"] in DISCOVERY_EVENT_TYPES),
            "first_invocations": len(actors_inv),
            "total_invocations": len(invokes),
            "successful_completions": sum(1 for e in invokes
                                          if e.get("outcome") == "success"),
            "repeat_callers": sum(1 for v in actors_inv.values() if len(v) > 1),
        }

    bindings = referral_bindings(store)
    organic_reg = [b for b in bindings if not b["first_party"]]
    total_new = [a for a in store.agents.values()
                 if not a.get("first_party") and not a.get("seed")]
    return {
        "genuine_external": funnel(ext),
        "unattributable_external": funnel(other),
        "ag_internal_first_party": funnel(fp),
        "machine_registrations_via_referral": len(organic_reg),
        "external_registrations_total": len(total_new),
        "pct_members_acquired_autonomously": (
            round(100 * len(organic_reg) / len(total_new), 1)
            if total_new else None),
        "cost_per_successful_external_acquisition": (
            "compute-only; ~$0 marginal (Render starter plan is the fixed cost)"),
    }
