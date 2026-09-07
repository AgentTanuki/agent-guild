"""L6 — discovery & referral graph: who discovered what, what they invoked,
and whether it led to registration. Organic vs AG-internal is decided by the
existing attribution layer (single source of truth) — AG-owned synthetic
interactions are excluded from every growth number by construction."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any

from ..attribution import attribution_class, is_genuine_external

DISCOVERY_EVENT_TYPES = {"swarm_index_fetch", "swarm_identity_fetch",
                         "swarm_terms_fetch"}
INVOKE_EVENT_TYPES = {"swarm_invoke"}


MEASUREMENT_NOTE = (
    "Event history uses the durable store where available. Restoring older "
    "records is not new activity; prior retained-tail snapshots are not "
    "comparable. External qualification is a caller heuristic, not proof of "
    "independent ownership. Completion means a utility returned success, not "
    "that a caller found it useful or paid. Historical missing attribution "
    "cannot be reconstructed. Compacted JSON history remains incomplete.")


def _swarm_events(store):
    return store.measurement_event_view(
        types=tuple(sorted(DISCOVERY_EVENT_TYPES | INVOKE_EVENT_TYPES)))


def public_actor_id(actor: str) -> str:
    """Public projection only: historical actor fields may contain secrets.

    Always fingerprint, including in legacy plaintext credential mode. Never
    depend on a current write-time sanitizer to protect immutable old rows.
    """
    digest = hashlib.sha256(("agent-guild/swarm-actor/v1\0" + str(actor)).encode()).hexdigest()
    return "swarm-actor:" + digest


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
    events, coverage = _swarm_events(store)
    for e in events:
        key = e.get("actor") or e.get("key") or "anon"
        a = actors.setdefault(key, {
            "actor": public_actor_id(key), "class": attribution_class(e),
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
        "measurement_version": "swarm-activity-v2",
        "actor_identifier_version": "swarm-actor-sha256-v1",
        "actor_identifier_note": (
            "Actor identifiers are stable domain-separated fingerprints, "
            "never raw historical credential or request actor fields. They "
            "identify recorded actor buckets, not independently owned agents."),
        "measurement_coverage": coverage,
        "interpretation": MEASUREMENT_NOTE,
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
    events, coverage = _swarm_events(store)
    buckets = {key: {"discovery_fetches": 0, "total_invocations": 0,
                     "successful_completions": 0, "actors": {}}
               for key in ("genuine_external", "unattributable_external",
                           "ag_internal_first_party")}
    for event in events:
        key = ("ag_internal_first_party" if event.get("fp") else
               "genuine_external" if is_genuine_external(event) else
               "unattributable_external")
        bucket = buckets[key]
        if event["type"] in DISCOVERY_EVENT_TYPES:
            bucket["discovery_fetches"] += 1
        else:
            bucket["total_invocations"] += 1
            bucket["successful_completions"] += event.get("outcome") == "success"
            actor = event.get("actor") or event.get("key") or "anon"
            bucket["actors"][actor] = bucket["actors"].get(actor, 0) + 1
    for bucket in buckets.values():
        actors = bucket.pop("actors")
        bucket["first_invocations"] = len(actors)
        bucket["repeat_callers"] = sum(n > 1 for n in actors.values())

    bindings = referral_bindings(store)
    organic_reg = [b for b in bindings if not b["first_party"]]
    total_new = [a for a in store.agents.values()
                 if not a.get("first_party") and not a.get("seed")]
    return {
        **buckets,
        "measurement_version": "swarm-activity-v2",
        "measurement_coverage": coverage,
        "interpretation": MEASUREMENT_NOTE,
        "machine_registrations_via_referral": len(organic_reg),
        "external_registrations_total": len(total_new),
        "pct_members_acquired_autonomously": (
            round(100 * len(organic_reg) / len(total_new), 1)
            if total_new else None),
        "cost_per_successful_external_acquisition": None,
        "cost_measurement": "unavailable: no allocated serving/acquisition cost ledger",
    }
