"""L5 — Machine Acquisition Gateway: the lowest-friction path for an external
agent to get real utility from Agent Guild.

Guest tier: no auth, terms inspectable BEFORE invocation (/terms.json), hard
per-actor daily budgets, payload caps, a global per-minute circuit breaker, and
an instant kill switch (env GUILD_SWARM_KILL=1 or admin POST /swarm/kill).
Members (any registered agent presenting its api_key) get a higher budget.
Every completion returns a signed provenance envelope (provenance.py). Joining
is never a hidden condition — invocation works without registration; terms say
exactly what membership adds."""
from __future__ import annotations

import hashlib
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Optional

import jsonschema

from .capabilities import CAPABILITIES, CapabilityError, run_capability, category_of
from .. import credentials as creds
from . import experience, provenance
from .identity import registry, SWARM_TAG

# --- limits (hard-coded budget ceilings; env can only lower them) -----------
GUEST_DAILY_LIMIT = int(os.environ.get("GUILD_SWARM_GUEST_DAILY", "200"))
MEMBER_DAILY_LIMIT = int(os.environ.get("GUILD_SWARM_MEMBER_DAILY", "2000"))
GLOBAL_PER_MINUTE = int(os.environ.get("GUILD_SWARM_GLOBAL_PER_MIN", "600"))
MAX_PAYLOAD_BYTES = 65536

GUEST_DAILY_LIMIT = min(GUEST_DAILY_LIMIT, 1000)
MEMBER_DAILY_LIMIT = min(MEMBER_DAILY_LIMIT, 10000)
GLOBAL_PER_MINUTE = min(GLOBAL_PER_MINUTE, 2000)


class Denied(Exception):
    def __init__(self, status: int, kind: str, detail: dict):
        self.status, self.kind, self.detail = status, kind, detail
        super().__init__(kind)


def swarm_killed(store) -> bool:
    if os.environ.get("GUILD_SWARM_KILL", "") == "1":
        return True
    return bool(store.swarm_state.get("killed"))


def set_killed(store, value: bool, reason: str = "") -> None:
    with store.lock:
        store.swarm_state["killed"] = bool(value)
        store.swarm_state["killed_reason"] = reason if value else ""
        store.swarm_state["killed_at"] = (
            datetime.now(timezone.utc).isoformat() if value else None)
        store._save()
    # Auditable operator event (op=True → caller class OPERATOR, structurally
    # excluded from every external-growth metric by the analytics invariant).
    store.record_event(None, "kill_switch_set" if value else "kill_switch_cleared",
                       op=True, reason=reason)


def derive_actor(x_api_key: Optional[str], client_host: str, ua: str,
                 store=None) -> tuple[str, bool]:
    """(actor_key, is_member). Members are keyed by their api_key (existing
    convention); guests get a stable ip+ua fingerprint namespaced 'swarm:'.

    Pilot A audit fix (2026-07-10): a presented key is only MEMBER if it
    belongs to a registered agent. Previously ANY non-empty X-API-Key string
    was granted the member tier (10× daily budget) and polluted member-keyed
    attribution. An unknown/revoked key now downgrades to guest (never a hard
    failure — guests are welcome) and is namespaced so it cannot collide with
    real member metrics."""
    if x_api_key:
        agent = (store.agent_for_presented_key(x_api_key)
                 if store is not None else None)
        if agent is not None:
            # member tier is a scoped privilege: a valid key without the
            # 'invoke' scope is denied explicitly (machine-readable), never
            # silently downgraded to guest.
            if not creds.has_scope(agent, "invoke"):
                raise Denied(403, "missing_scope",
                             creds.scope_error(agent, "invoke"))
            return (creds.actor_key_for_agent(agent) or x_api_key), True
        fp = hashlib.sha256(f"{client_host}|{ua}".encode()).hexdigest()[:16]
        return f"swarm:badkey:{fp}", False
    fp = hashlib.sha256(f"{client_host}|{ua}".encode()).hexdigest()[:16]
    return f"swarm:{fp}", False


# in-memory rate state (buckets reset on restart — acceptable for the pilot;
# hard global budget still bounds worst-case volume)
_day_buckets: dict[str, tuple[str, int]] = {}
_minute_window: list[float] = []


def _check_rate(actor: str, is_member: bool) -> dict:
    now = time.time()
    # global circuit breaker
    cutoff = now - 60
    while _minute_window and _minute_window[0] < cutoff:
        _minute_window.pop(0)
    if len(_minute_window) >= GLOBAL_PER_MINUTE:
        raise Denied(429, "global_rate", {
            "error": "global rate limit", "retry_after_seconds": 60})
    today = datetime.now(timezone.utc).date().isoformat()
    day, used = _day_buckets.get(actor, (today, 0))
    if day != today:
        used = 0
    limit = MEMBER_DAILY_LIMIT if is_member else GUEST_DAILY_LIMIT
    if used >= limit:
        raise Denied(429, "daily_budget", {
            "error": "daily invocation budget exhausted",
            "limit": limit, "tier": "member" if is_member else "guest",
            "retry_after_seconds": 86400,
            "raise_limit": "register (POST /agents/register) and present your "
                           "api_key as X-API-Key for the member budget",
        })
    _minute_window.append(now)
    _day_buckets[actor] = (today, used + 1)
    return {"tier": "member" if is_member else "guest", "limit": limit,
            "used_today": used + 1, "remaining_today": limit - used - 1}


def _payload_size_ok(payload: Any) -> int:
    import json
    size = len(json.dumps(payload, separators=(",", ":")).encode())
    if size > MAX_PAYLOAD_BYTES:
        raise Denied(413, "payload_too_large", {
            "error": "payload too large", "max_bytes": MAX_PAYLOAD_BYTES,
            "got_bytes": size})
    return size


def invoke(store, capability_id: str, payload: Any, *,
           x_api_key: Optional[str], client_host: str, ua: str,
           first_party: bool, base: str,
           first_party_role: str = "internal") -> tuple[int, dict]:
    """The one chokepoint every invocation flows through:
    kill switch → capability lookup → payload cap → rate limit → schema
    validation → run → provenance + experience + instrumentation."""
    if swarm_killed(store):
        raise Denied(503, "kill_switch", {
            "error": "swarm invocations are disabled",
            "reason": store.swarm_state.get("killed_reason") or "operator kill switch"})
    cap = CAPABILITIES.get(capability_id)
    doc = registry.for_capability(capability_id) if cap else None
    if cap is None or doc is None:
        raise Denied(404, "unknown_capability", {
            "error": f"unknown or unpublished capability {capability_id!r}",
            "index": f"{base}/.well-known/ag-identities/index.json"})
    if not isinstance(payload, dict):
        raise Denied(422, "bad_payload", {
            "error": "payload must be a JSON object matching input_schema",
            "input_schema": cap.input_schema})
    _payload_size_ok(payload)
    actor, is_member = derive_actor(x_api_key, client_host, ua, store=store)
    rate = _check_rate(actor, is_member)

    invocation_id = "inv_" + secrets.token_hex(8)
    ok, error_kind, output, latency_ms = True, None, None, 0.0
    try:
        output, latency_ms = run_capability(capability_id, payload)
    except jsonschema.ValidationError as e:
        ok, error_kind = False, "schema_validation"
        output = {"error": "payload failed input_schema validation",
                  "message": e.message,
                  "path": "/" + "/".join(str(p) for p in e.absolute_path),
                  "input_schema": cap.input_schema}
    except CapabilityError as e:
        ok, error_kind = False, "unprocessable"
        output = {"error": str(e)}
    except Exception:  # noqa: BLE001 — a capability bug must return structured error, not 500
        ok, error_kind = False, "internal"
        output = {"error": "internal capability error (recorded)"}

    referral = provenance.new_referral_token() if ok and not is_member else None
    envelope = provenance.build_envelope(
        guild_identity=store.guild_identity(), base=base,
        ag_id=doc["identity"]["ag_id"], capability_id=capability_id,
        capability_version=cap.version, invocation_id=invocation_id,
        ok=ok, latency_ms=latency_ms, cost_credits=0,
        referral_token=referral, error_kind=error_kind)

    with store.lock:
        # instrumentation: flows through the existing attribution pipeline, so
        # genuine-external vs first-party is decided by the same code as
        # everything else (T12: false-demand exclusion).
        store.record_event(
            None if not is_member else actor, "swarm_invoke", ua=ua,
            endpoint="swarm_invoke", capability=capability_id,
            outcome="success" if ok else error_kind,
            tier=rate["tier"], actor=actor)
        if first_party:
            # record_event derives fp from the billing account; guests have
            # none, so tag explicitly when the caller declared first-party.
            store.events[-1]["fp"] = True
            store.events[-1]["fp_role"] = first_party_role
        if referral:
            tokens = store.swarm_state.setdefault("referral_tokens", {})
            tokens[referral] = {"capability": capability_id, "actor": actor,
                                "invocation_id": invocation_id,
                                "issued_at": datetime.now(timezone.utc).isoformat()}
            if len(tokens) > 5000:
                for k in list(tokens)[: len(tokens) - 5000]:
                    tokens.pop(k, None)
        counters = store.swarm_state.setdefault("counters", {})
        c = counters.setdefault(capability_id, {"invocations": 0, "successes": 0,
                                                "failures": 0, "caller_errors": 0,
                                                "total_latency_ms": 0.0})
        c["invocations"] += 1
        if ok:
            c["successes"] += 1
        elif error_kind == "internal":
            c["failures"] += 1            # OUR bug — counts against reliability
        else:
            # schema_validation / unprocessable = the CALLER's payload; a
            # correct structured rejection is not a capability failure
            c.setdefault("caller_errors", 0)
            c["caller_errors"] += 1
        c["total_latency_ms"] = round(c["total_latency_ms"] + latency_ms, 3)
        from ..attribution import attribution_class
        experience.append(store.swarm_state, experience.build_record(
            capability_id=capability_id, category=category_of(capability_id),
            payload=payload, ok=ok, latency_ms=latency_ms,
            failure_kind=error_kind,
            caller_class=attribution_class(store.events[-1])))
        # bounded persistence: full save every 20 invocations (events
        # themselves are journal-durable already)
        n = store.swarm_state.get("_since_save", 0) + 1
        if n >= 20:
            store.swarm_state["_since_save"] = 0
            store._save()
        else:
            store.swarm_state["_since_save"] = n

    status = 200 if ok else (422 if error_kind in ("schema_validation", "unprocessable") else 500)
    return status, {
        "ok": ok,
        "capability": capability_id,
        "version": cap.version,
        "invocation_id": invocation_id,
        "result": output,
        "latency_ms": round(latency_ms, 3),
        "rate": rate,
        "provenance": envelope,
        "next": {
            "related": f"{base}/.well-known/ag-identities/index.json",
            "terms": f"{base}/terms.json",
            "join": {"method": "POST", "path": "/agents/register",
                     "why": "member budget, a listed identity, portable "
                            "reputation, and the trust graph",
                     "referral": ("include metadata.referral_token="
                                  + referral) if referral else None},
        },
    }


def terms(base: str) -> dict:
    """Machine-readable member and non-member terms — inspectable BEFORE any
    invocation, never inserted after task data has been supplied."""
    return {
        "schema_version": "ag-terms/1",
        "service": "Agent Guild — Discovery Swarm capabilities",
        "guest_tier": {
            "auth": "none",
            "cost": "free",
            "daily_invocation_limit": GUEST_DAILY_LIMIT,
            "max_payload_bytes": MAX_PAYLOAD_BYTES,
            "provenance": "every completion returns a Guild-signed envelope",
            "data_retention": provenance.RETENTION_STATEMENT,
            "no_hidden_conditions": "invocation never requires registration; "
                                    "these terms do not change mid-session",
        },
        "member_tier": {
            "join": {"method": "POST", "path": "/agents/register", "cost": "free",
                     "human_required": False},
            "auth": "X-API-Key (issued at registration)",
            "daily_invocation_limit": MEMBER_DAILY_LIMIT,
            "adds": ["higher invocation budget",
                     "a public listed identity on the answer surfaces",
                     "the proving rung -> guild-observed evidence",
                     "portable Guild-signed Agent Passport",
                     "escrowed agent-to-agent work + attestations"],
            "trust_journey": f"{base}/citizenship",
        },
        "prohibited": ["payloads you have no right to process",
                       "resource-exhaustion or injection attempts",
                       "reselling guest access as your own paid capability "
                       "without provenance passthrough"],
        "abuse_handling": "rate limits + circuit breakers; violating keys are "
                          "revoked via POST /agents/{id}/key/revoke (members "
                          "may also revoke/rotate their own key at "
                          "/agents/{id}/key/rotate)",
        "kill_switch": "the operator can disable all swarm invocations "
                       "instantly; you will receive HTTP 503",
        "terms_stability": "terms are versioned; this document is the "
                           "machine-readable source of truth",
    }
