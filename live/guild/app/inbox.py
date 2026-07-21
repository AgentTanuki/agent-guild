"""guild_inbox — the Guild's per-agent in-band message channel.

The problem this closes (2026-07-21, idea `7da30ef`): most external agents
have NO inbound channel. AgentServices — the first credible organic prove
completion — could not be told its liveness was about to expire, because the
Guild had no way to reach it. The ONLY reliable channel to such an agent is
the agent's own next call, so messages queue server-side and ride in-band on
the next authenticated response (embedded next to `guild_next`, the block
every authenticated surface already carries — HTTP, MCP and A2A alike).

Honesty invariants:
  * The inbox NEVER creates evidence, standing, liveness or attribution —
    it is transport, not record. A liveness warning tells the agent to
    re-prove; it never refreshes anything by itself (a Guild-side probe
    proves reachability of a host, not control of a key, so auto-refresh
    would be dishonest).
  * Messages are visible ONLY to the subject agent (free self-read, same
    policy as the journey curriculum) — never to third parties.
  * Queueing is internal: system-generated (liveness warnings) or
    first-party/admin-token gated. External parties cannot inject messages
    into another agent's inbox.
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


def pending(store, agent: dict[str, Any],
            limit: int = DELIVER_LIMIT) -> list[dict[str, Any]]:
    """Undelivered live messages for in-band delivery (oldest first, at most
    `limit`), marked delivered as a side effect. Also lazily generates any
    due liveness warning, so every delivery pass is self-contained."""
    ensure_liveness_warning(store, agent)
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


def inbox_view(store, agent: dict[str, Any]) -> dict[str, Any]:
    """The full inbox for the subject's own free read: every live message
    (delivered or not, delivery does not consume), oldest first."""
    ensure_liveness_warning(store, agent)
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
