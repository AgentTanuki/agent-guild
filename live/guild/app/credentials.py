"""Credential hardening (Pilot A audit, 2026-07-10) — salted-hashed api keys,
public key_ids, least-privilege scopes and expiry.

Gated behind GUILD_HASH_KEYS:
  * unset / != "1"  -> legacy behavior, byte-for-byte: plaintext api_key on the
    record; accounts and events keyed by the raw key.
  * "1"             -> new issues store ONLY a salted PBKDF2 verifier + a public
    key_id; the raw key is returned exactly once at issuance. Accounts and event
    actor keys use the key_id. Existing plaintext keys migrate in place on load.

Verifier construction (per the credential-hardening refinements): a raw,
unsalted digest is NOT used. api keys are high-entropy (sk_ + 24 random bytes =
192 bits), so brute force is already infeasible; PBKDF2-HMAC-SHA256 with a
per-key random salt is applied for defense-in-depth and to defeat any
precomputation / cross-key correlation. Format, self-describing so the cost can
be raised later without a migration:

    pbkdf2_sha256$<iterations>$<salt_b64>$<dk_b64>

Design + rollout: docs/discovery-swarm/CREDENTIALS_DESIGN.md
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import datetime, timezone
from typing import Any, Optional

# Closed scope vocabulary. Unknown scopes fail closed (see has_scope).
SCOPES = ("read", "invoke", "attest", "escrow", "admin")

# Least privilege for a newly issued credential: every action a normal member
# legitimately performs, but NEVER `admin`. admin is reserved for the operator
# path (admin token) and is never granted to a self-registered key.
DEFAULT_ISSUE_SCOPES = ("read", "invoke", "attest", "escrow")

# Backward compatibility: a record that predates scoping has no `scopes` field
# and is treated as fully privileged until its next rotation re-issues it with
# least-privilege scopes. New records always carry an explicit `scopes` list.
_LEGACY_ALL_SCOPES = tuple(SCOPES)

KEY_ID_LEN = 12  # hex chars — public, stable identifier (NOT the verifier)

_DEFAULT_ITERS = int(os.environ.get("GUILD_KDF_ITERS", "100000"))


def hashing_enabled() -> bool:
    """Read the flag at call time (never cached at import) so tests and
    operators can flip modes without a process restart."""
    return os.environ.get("GUILD_HASH_KEYS", "") == "1"


# --- verifier (salted KDF) ---------------------------------------------------

def hash_key(raw: str, *, iterations: Optional[int] = None) -> str:
    """Salted PBKDF2-HMAC-SHA256 verifier for a raw key. Non-deterministic
    (fresh random salt each call) — NOT an identifier; use key_id_of for that."""
    iters = iterations or _DEFAULT_ITERS
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt, iters)
    return "pbkdf2_sha256${}${}${}".format(
        iters,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_key_hash(raw: str, stored: str) -> bool:
    """Constant-time verification of a raw key against a stored verifier.
    Understands the pbkdf2_sha256 format and, defensively, a legacy bare
    sha256 hex digest (so records hashed by the pre-refinement branch still
    verify during a rolling upgrade)."""
    if not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, iters_s, salt_b64, dk_b64 = stored.split("$", 3)
            iters = int(iters_s)
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(dk_b64)
        except (ValueError, base64.binascii.Error):
            return False
        dk = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt, iters)
        return hmac.compare_digest(dk, expected)
    # legacy bare sha256 hex (pre-refinement records): verify, so a rolling
    # re-hash can happen on next rotation without locking anyone out.
    if len(stored) == 64 and all(c in "0123456789abcdef" for c in stored):
        return hmac.compare_digest(
            hashlib.sha256(raw.encode("utf-8")).hexdigest(), stored)
    return False


def key_id_of(raw: str) -> str:
    """Public identifier for a secret key: first 12 hex chars of sha256(key).
    This is an IDENTIFIER, not a verifier — it derives no security from
    secrecy, is safe to log / use as a dict key, and cannot authenticate
    (account resolution never accepts a bare key_id; see Store._account_key).
    Deterministic so a presented raw key can be looked up in O(1)."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:KEY_ID_LEN]


def sanitize_actor_key(key: Optional[str]) -> Optional[str]:
    """Map a raw secret (sk_...) to its public key_id when hashing is ON, so no
    secret can reach the events journal — even a probe with an unknown or
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
    storage forms (plaintext legacy, salted hash) in either mode, plus
    revocation (no stored credential) and optional expiry."""
    if not agent or not presented:
        return False
    if _expired(agent):
        return False
    stored_hash = agent.get("api_key_hash")
    if stored_hash:
        return verify_key_hash(presented, stored_hash)
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
    """A record WITH a `scopes` field returns exactly those (new records carry
    least-privilege scopes). A record WITHOUT one is a pre-scoping legacy
    record and is treated as fully privileged until its next rotation — this
    keeps existing agents working, and is the only place 'all scopes' is the
    default."""
    if not agent:
        return list(DEFAULT_ISSUE_SCOPES)
    s = agent.get("scopes")
    if s is None:
        return list(_LEGACY_ALL_SCOPES)
    return list(s)


def has_scope(agent: Optional[dict[str, Any]], scope: str) -> bool:
    """Fail closed: an unknown scope (outside the vocabulary) is never granted,
    even to a legacy all-scopes record."""
    if scope not in SCOPES:
        return False
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
