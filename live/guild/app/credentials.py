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

# Legacy policy (refinement 2026-07-10): a record that predates scoping has no
# `scopes` field. It is NOT treated as fully privileged. It receives the normal
# least-privilege member set (read/invoke/attest/escrow) — never admin — until
# its next rotation writes an explicit modern scope set. is_legacy_scope() lets
# the store record a one-time `legacy_credential_used` audit event and expose an
# operator-visible count of credentials still on legacy interpretation.
LEGACY_SCOPES = tuple(DEFAULT_ISSUE_SCOPES)


def is_legacy_scope(agent: Optional[dict[str, Any]]) -> bool:
    """True iff this record relies on the legacy scope interpretation (exists,
    but carries no explicit `scopes` field)."""
    return bool(agent) and agent.get("scopes") is None

KEY_ID_LEN = 32  # hex chars = 128 bits of identifier entropy (NOT a secret)

DK_LEN = 32                 # derived-key length in bytes (explicit)
MIN_PROD_ITERS = 100_000    # production floor; below this needs dev/test mode
MAX_ITERS = 10_000_000      # bound any iteration count before it drives PBKDF2


def _weak_kdf_allowed() -> bool:
    """Sub-floor iteration counts (fast tests) are accepted ONLY in a clearly
    recognised dev/test environment."""
    return os.environ.get("GUILD_ALLOW_WEAK_KDF", "") == "1"


def _configured_iters() -> int:
    """Iteration count for NEW hashes. A configured value below the production
    floor is clamped UP to the floor unless dev/test mode is explicit, so a
    misconfiguration can never silently weaken production. Always bounded by
    MAX_ITERS."""
    try:
        raw = int(os.environ.get("GUILD_KDF_ITERS", str(MIN_PROD_ITERS)))
    except ValueError:
        raw = MIN_PROD_ITERS
    if raw < MIN_PROD_ITERS and not _weak_kdf_allowed():
        raw = MIN_PROD_ITERS
    return max(1, min(raw, MAX_ITERS))


def hashing_enabled() -> bool:
    """Read the flag at call time (never cached at import) so tests and
    operators can flip modes without a process restart."""
    return os.environ.get("GUILD_HASH_KEYS", "") == "1"


# --- verifier (salted KDF) ---------------------------------------------------

def hash_key(raw: str, *, iterations: Optional[int] = None) -> str:
    """Salted PBKDF2-HMAC-SHA256 verifier for a raw key. Non-deterministic
    (fresh CSPRNG salt each call) — NOT an identifier; use key_id_of for that.
    AG keys are high-entropy machine-generated tokens (sk_ + 24 random bytes =
    192 bits), NOT human-selected passwords; PBKDF2 is defense-in-depth, not the
    primary barrier (see CREDENTIALS_DESIGN.md §13)."""
    iters = iterations if iterations is not None else _configured_iters()
    iters = max(1, min(iters, MAX_ITERS))
    salt = os.urandom(16)  # os.urandom = CSPRNG
    dk = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt, iters, dklen=DK_LEN)
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
        # Bound the stored/parsed iteration count BEFORE it drives PBKDF2 so a
        # corrupt or tampered verifier can never cause an unbounded computation.
        if not (1 <= iters <= MAX_ITERS):
            return False
        dk = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt, iters,
                                 dklen=len(expected) or DK_LEN)
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
    """Effective scopes for a record, fail-closed throughout:
      * no agent            -> [] (no credential, no scopes);
      * `scopes` absent      -> LEGACY_SCOPES (least-privilege member set);
      * `scopes` is a list   -> only its KNOWN entries (unknown/malformed
                                values are dropped = fail closed per value);
      * `scopes` malformed    -> [] (corrupt record grants nothing; re-issue).
    admin is never implied — it must be listed explicitly."""
    if not agent:
        return []
    s = agent.get("scopes")
    if s is None:
        return list(LEGACY_SCOPES)
    if not isinstance(s, (list, tuple)):
        return []
    return [x for x in s if isinstance(x, str) and x in SCOPES]


def has_scope(agent: Optional[dict[str, Any]], scope: str) -> bool:
    """Fail closed: an unknown scope (outside the vocabulary) is never granted."""
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
