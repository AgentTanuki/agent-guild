"""guild_inbox — the Guild's per-agent in-band message channel.

The problem this closes (2026-07-21, idea `7da30ef`): most external agents
have NO inbound channel. AgentServices — the first credible organic prove
completion — could not be told its liveness was about to expire, because the
Guild had no way to reach it. The ONLY reliable channel to such an agent is
the agent's own next call, so messages queue server-side and ride in-band.

DELIVERY SURFACES — the precise set (corrective pass 2026-07-22; the earlier
"every authenticated surface — HTTP, MCP and A2A alike" claim overstated what
shipped, and claims must match code exactly):
  * HTTP: every response that embeds `guild_next` (register, configuration,
    endpoint declaration, prove verify, receipt, attestation, escrow release,
    demand watch), the subject's free self-read of its own journey
    (GET /agents/{id}/journey with its key), and the inbox itself
    (GET /agents/{id}/inbox).
  * MCP: EVERY tool call whose `api_key` argument authenticates a subject
    agent (delivery keys off the credential, not off the specific tool).
  * A2A: every message/send carrying the agent's X-API-Key header.
An interaction that presents no subject credential (an anonymous read, a
guest swarm invocation without api_key) carries nothing — the inbox is
private correspondence and is never exposed on an unauthenticated path.

Honesty invariants:
  * The inbox NEVER creates evidence, standing, liveness or attribution —
    it is transport, not record. A liveness warning tells the agent to
    re-prove; it never refreshes anything by itself (a Guild-side probe
    proves reachability of a host, not control of a key, so auto-refresh
    would be dishonest).
  * Messages are visible ONLY to the subject agent (free self-read, same
    policy as the journey curriculum) — never to third parties.
  * Queueing is internal: system-generated (liveness warnings, the one-time
    passport offer) or first-party/admin-token gated. External parties
    cannot inject messages into another agent's inbox.
  * Bounded: per-agent cap, per-message expiry — the inbox can never grow
    without limit or serve stale instructions forever.

Import discipline: like journey.py, this module never imports the store —
it receives the instance — so store.py stays import-cycle-free.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# Warn this many days before proof-of-conduct liveness expires (and keep
# warning after expiry until the agent re-proves).
LIVENESS_WARN_DAYS = 7
# Default message time-to-live; expired messages are dropped at read time.
DEFAULT_TTL_DAYS = 45
# Per-agent queue bound: oldest messages are dropped first past this size.
MAX_MESSAGES_PER_AGENT = 20
# How many undelivered messages ride in-band on one response.
DELIVER_LIMIT = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(ts: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _alive(msg: dict[str, Any]) -> bool:
    exp = _parse(msg.get("expires_at") or "")
    return exp is not None and exp > _now()


def queue_message(store, agent_id: str, *, topic: str, body: str,
                  action: Optional[dict[str, Any]] = None,
                  ttl_days: int = DEFAULT_TTL_DAYS,
                  dedupe_key: Optional[str] = None,
                  source: str = "guild_system") -> Optional[dict[str, Any]]:
    """Queue one message for one agent. With dedupe_key, a live message
    carrying the same key makes this a no-op (returns None) — so recurring
    generators (liveness warnings) can call this idempotently. Oldest
    messages are dropped past MAX_MESSAGES_PER_AGENT."""
    box = store.guild_inbox.setdefault(agent_id, [])
    box[:] = [m for m in box if _alive(m)]
    if dedupe_key and any(m.get("dedupe_key") == dedupe_key for m in box):
        return None
    msg = {
        "id": "msg_" + secrets.token_hex(8),
        "created_at": _iso(_now()),
        "expires_at": _iso(_now() + timedelta(days=ttl_days)),
        "topic": topic,
        "body": body,
        "action": action,
        "source": source,
        "dedupe_key": dedupe_key,
        "delivered_at": None,
    }
    box.append(msg)
    if len(box) > MAX_MESSAGES_PER_AGENT:
        del box[: len(box) - MAX_MESSAGES_PER_AGENT]
    store.record_event(None, "inbox_queued", agent_id=agent_id,
                       topic=topic, message_id=msg["id"])
    store._save()
    return msg


def ensure_liveness_warning(store, agent: dict[str, Any]) -> None:
    """Idempotently queue a liveness warning when the agent's
    proof-of-conduct expires within LIVENESS_WARN_DAYS (or has already
    expired). The dedupe key pins the exact expiry timestamp, so each
    proving cycle warns at most once; a refresh (new expiry) re-arms it."""
    proof = (agent or {}).get("proof_of_conduct") or {}
    expires = _parse(proof.get("liveness_expires_at") or "")
    if expires is None:
        return
    if expires - timedelta(days=LIVENESS_WARN_DAYS) > _now():
        return
    aid = agent["id"]
    from . import journey as journey_engine
    expired = expires <= _now()
    state = ("EXPIRED " + proof.get("liveness_expires_at", "")
             if expired else
             "expires " + proof.get("liveness_expires_at", ""))
    queue_message(
        store, aid,
        topic="liveness_expiry",
        body=(f"Your proof-of-conduct liveness {state}. A stale proof reads "
              "as an unknown record to cautious verifiers. One "
              "challenge-response refreshes it — timestamps only, it never "
              "mints new work evidence."),
        action={
            "action": "refresh_liveness",
            "call": (f"POST {journey_engine.BASE}/agents/{aid}/prove → sign "
                     f"the challenge → POST {journey_engine.BASE}/agents/"
                     f"{aid}/prove/verify"),
        },
        ttl_days=DEFAULT_TTL_DAYS,
        dedupe_key=f"liveness:{proof.get('liveness_expires_at')}",
    )


def ensure_passport_offer(store, agent: dict[str, Any]) -> None:
    """Idempotently queue the ONE passport offer for an agent that has proved
    control (prove_completed milestone) but never fetched its passport (no
    first_passport). Once per counterparty FOREVER: queue_message's dedupe_key
    only spans LIVE messages, so the `passport_offer_sent` milestone (durable,
    once-per-agent) guards re-queueing after the message expires — an agent
    that ignored the offer is never spammed with it again."""
    ms = (agent or {}).get("milestones") or {}
    if "prove_completed" not in ms or "first_passport" in ms:
        return
    if "passport_offer_sent" in ms:
        return
    aid = agent["id"]
    from . import journey as journey_engine
    base = journey_engine.BASE
    msg = queue_message(
        store, aid,
        topic="Claim your Agent Guild passport",
        body=(f"You proved control — your record is now worth carrying. "
              f"1) GET {base}/agents/{aid}/passport (free) returns a "
              "Guild-signed Verifiable Credential of your standing. "
              f"2) Any party verifies it offline against the Guild's "
              f"published did ({base}/.well-known/agent-guild-did.json) or "
              f"live via POST {base}/credentials/verify "
              '{"credential": <the passport JSON>}. '
              f"3) Embed your live badge: {base}/agents/{aid}/badge.svg. "
              "4) Expose it: add the badge and your passport URL to your own "
              "agent card, manifest, or service metadata, so counterparties "
              "can verify you without asking the Guild."),
        action={"method": "GET", "url": f"{base}/agents/{aid}/passport"},
        dedupe_key=f"passport_offer:{aid}",
        source="guild_system",
    )
    if msg is not None:
        # durable once-ever guard (milestones never repeat); also lands the
        # offer in the acquisition telemetry via the milestone event.
        if store.record_milestone(aid, "passport_offer_sent"):
            store._save()


def pending(store, agent: dict[str, Any],
            limit: int = DELIVER_LIMIT) -> list[dict[str, Any]]:
    """Undelivered live messages for in-band delivery (oldest first, at most
    `limit`), marked delivered as a side effect. Also lazily generates any
    due liveness warning (and the one-time passport offer), so every delivery
    pass is self-contained."""
    ensure_liveness_warning(store, agent)
    ensure_passport_offer(store, agent)
    box = [m for m in store.guild_inbox.get(agent["id"], []) if _alive(m)]
    out = [m for m in box if not m.get("delivered_at")][:limit]
    if not out:
        return []
    now = _iso(_now())
    for m in out:
        m["delivered_at"] = now
        store.record_event(None, "inbox_delivered", agent_id=agent["id"],
                           topic=m.get("topic"), message_id=m.get("id"))
    store._save()
    return [_public(m) for m in out]


def deliver_in_band(store, agent: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The in-band delivery block (the same shape guild_next embeds), or None
    when nothing is pending. One helper so every transport delivers
    identically and the delivered_at bookkeeping lives in exactly one place."""
    msgs = pending(store, agent)
    if not msgs:
        return None
    from . import journey as journey_engine
    return {
        "messages": msgs,
        "read_all": (f"GET {journey_engine.BASE}/agents/{agent['id']}/inbox "
                     "(free to you)"),
    }


def subject_for_presented_key(store, presented: Optional[str]
                              ) -> Optional[dict[str, Any]]:
    """Resolve a PRESENTED credential to the agent it authenticates, or None.

    Authentication-grade on purpose — the inbox is private correspondence.
    Exactly the two forms the REST self-read rule (main._is_self_read)
    accepts: the agent's own credential (Store.agent_for_presented_key —
    constant-time compare, revocation- and expiry-aware), or a billing key
    resolving through Store._account_key (which never accepts a bare public
    key_id) to the account that owns the agent."""
    if not presented:
        return None
    agent = store.agent_for_presented_key(presented)
    if agent is not None:
        return agent
    key = store._account_key(presented)
    acct = store.accounts.get(key) if key else None
    aid = (acct or {}).get("owner_agent_id")
    return store.agents.get(aid) if aid else None


def inbox_view(store, agent: dict[str, Any]) -> dict[str, Any]:
    """The full inbox for the subject's own free read: every live message
    (delivered or not, delivery does not consume), oldest first."""
    ensure_liveness_warning(store, agent)
    ensure_passport_offer(store, agent)
    box = store.guild_inbox.get(agent["id"], [])
    live = [m for m in box if _alive(m)]
    if len(live) != len(box):
        store.guild_inbox[agent["id"]] = live
        store._save()
    return {
        "agent_id": agent["id"],
        "messages": [_public(m) for m in live],
        "count": len(live),
        "note": ("Messages from the Guild, delivered in-band because you "
                 "have no inbound endpoint on file. Reading is free to you "
                 "and visible to no one else. Delivery never consumes a "
                 "message; expiry does."),
    }


def _public(msg: dict[str, Any]) -> dict[str, Any]:
    return {k: msg.get(k) for k in
            ("id", "created_at", "expires_at", "topic", "body", "action",
             "source", "delivered_at")}
