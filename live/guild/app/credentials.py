"""Credential hardening (Pilot A audit, 2026-07-10) — hashed-at-rest api keys,
public key_ids, scopes and expiry.

Everything here is gated behind the env flag GUILD_HASH_KEYS:

  * unset / anything but "1"  -> legacy behavior, byte-for-byte: plaintext
    api_key on the agent record, accounts and events keyed by the raw key.
  * "1"                       -> new issues store sha256(api_key) only; the
    raw key is returned exactly once at issuance. Accounts and event actor
    keys use the public, stable `key_id` (first 12 hex chars of the hash).
    Existing plaintext keys are migrated in place on first Store load.

Design: docs/discovery-swarm/CREDENTIALS_DESIGN.md
"""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone
from typing import Any, Optional

# Closed scope vocabulary. Every key defaults to ALL scopes (full backward
# compatibility); an agent record may narrow itself via its `scopes` list.
SCOPES = ("read", "invoke", "attest", "escrow", "admin")
DEFAULT_SCOPES = list(SCOPES)

KEY_ID_LEN = 12  # hex chars of sha256 — public, stable, non-reversible


def hashing_enabled() -> bool:
    """Read the flag at call time (never cached at import) so tests and
    operators can flip modes without a process restart."""
    return os.environ.get("GUILD_HASH_KEYS", "") == "1"


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def key_id_of(raw: str) -> str:
    """Public identifier for a secret key: first 12 hex chars of its sha256.
    Safe to log, safe to use as a dict key, useless as a credential (account
    resolution never accepts a bare key_id — see Store._account_key)."""
    return hash_key(raw)[:KEY_ID_LEN]


def sanitize_actor_key(key: Optional[str]) -> Optional[str]:
    """Map a raw secret (sk_...) to its public key_id when hashing is ON, so
    no secret can reach the events journal — even a probe with an unknown or
    mistyped sk_ key is logged pseudonymously, never verbatim."""
    if key and hashing_enabled() and key.startswith("sk_"):
        return key_id_of(key)
    return key


def agent_has_active_key(agent: dict[str, Any]) -> bool:
    """True if the agent holds an issuable credential (plaintext or hashed).
    Revocation clears both fields."""
    return bool(agent.get("api_key") or agent.get("api_key_hash"))


def _expired(agent: dict[str, Any]) -> bool:
    exp = agent.get("api_key_expires_at")
    if not exp:
        return False
    try:
        dt = datetime.fromisoformat(exp)
    except ValueError:
        return False
    return datetime.now(timezone.utc) >= dt


def verify_agent_key(agent: Optional[dict[str, Any]], presented: Optional[str]) -> bool:
    """Constant-time credential check against one agent record. Handles both
    storage forms (plaintext legacy, sha256 hash) in either mode, plus
    revocation (no stored credential) and optional expiry."""
    if not agent or not presented:
        return False
    if _expired(agent):
        return False
    stored_hash = agent.get("api_key_hash")
    if stored_hash:
        return hmac.compare_digest(hash_key(presented), stored_hash)
    stored = agent.get("api_key")
    if stored:
        return hmac.compare_digest(presented.encode("utf-8"), stored.encode("utf-8"))
    return False


def actor_key_for_agent(agent: dict[str, Any]) -> Optional[str]:
    """The key this agent's account and events are keyed by: key_id once
    hashed, the raw api_key while still plaintext (legacy mode)."""
    return agent.get("key_id") or agent.get("api_key")


# --- scopes ------------------------------------------------------------------

def scopes_of(agent: Optional[dict[str, Any]]) -> list[str]:
    """An agent record without a `scopes` field has ALL scopes — existing
    records keep working unchanged."""
    if not agent:
        return DEFAULT_SCOPES
    s = agent.get("scopes")
    if s is None:
        return DEFAULT_SCOPES
    return list(s)


def has_scope(agent: Optional[dict[str, Any]], scope: str) -> bool:
    return scope in scopes_of(agent)


def scope_error(agent: Optional[dict[str, Any]], scope: str) -> dict[str, Any]:
    """Machine-readable 403 body naming exactly the missing scope."""
    return {
        "error": "missing_scope",
        "required_scope": scope,
        "have_scopes": scopes_of(agent),
        "agent_id": (agent or {}).get("id"),
        "detail": f"this credential does not carry the '{scope}' scope",
    }


class MissingScope(Exception):
    """Raised by store-level checks; HTTP layers map it to a 403."""

    def __init__(self, agent: Optional[dict[str, Any]], scope: str):
        self.detail = scope_error(agent, scope)
        super().__init__(self.detail["detail"])
