"""agent-guild/externality-attestation/v1 — INDEPENDENT proof of externality.

Why this module exists (machine-integrity correction, 2026-07-17): a
self-created did:key plus a self-controlled wallet proves machine identity
continuity and wallet control. It does NOT prove the payer is external to
Agent Guild — an Agent Guild-controlled process can mint both in seconds.
Externality is therefore a claim that only a SEPARATE, allowlisted issuer can
establish, by signing an explicit attestation about the subject DID.

An attestation is a JCS/Ed25519-signed document:

    v            "agent-guild/externality-attestation/v1"
    subject_did  the machine DID whose externality is being attested
    issuer_did   the attesting issuer's did:key (NEVER the Guild, NEVER the
                 subject itself)
    claim        "operated_outside_agent_guild"
    issued_at / expires_at   ISO-8601 validity window
    proof        Ed25519 over JCS(document minus proof) by the issuer key

Classification honesty:
  * the ALLOWLIST (GUILD_EXTERNALITY_ATTESTOR_DIDS, comma-separated did:key)
    is EMPTY by default — until a real independent attestor exists and is
    explicitly configured, `independently_attested_external_machine` totals
    are honestly ZERO;
  * a stored attestation is re-validated at READ time (signature, allowlist,
    subject, window, independence) — putting a document in the store can
    never by itself create external revenue.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

from . import crypto

PROTOCOL = "agent-guild/externality-attestation/v1"
CLAIM = "operated_outside_agent_guild"
MAX_ATTESTATIONS = 10_000


def allowlisted_issuers() -> set[str]:
    """The operator-configured set of independent attestor DIDs. Empty by
    default: no allowlist ⇒ nothing can be independently attested."""
    raw = os.environ.get("GUILD_EXTERNALITY_ATTESTOR_DIDS") or ""
    return {d.strip() for d in raw.split(",") if d.strip()}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


#: tolerated forward clock skew for an issuer's `issued_at` (seconds)
ISSUED_AT_SKEW_S = 120.0


def _parse_rfc3339(value: Any) -> Optional[datetime]:
    """STRICT timezone-aware RFC 3339 parse for UNTRUSTED issuer
    timestamps. Returns None for anything that is not a string parsing to
    a timezone-AWARE datetime — naive datetimes, garbage, numbers and
    empty values all fail. Lexicographic string comparison is never used
    on these fields (differing offsets/precision would order wrongly)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return None                       # naive timestamps are rejected
    return dt


def _valid_shape(att: Any) -> bool:
    if not isinstance(att, dict):
        return False
    for k in ("v", "subject_did", "issuer_did", "claim", "issued_at",
              "expires_at", "proof"):
        if not att.get(k):
            return False
    return att["v"] == PROTOCOL


def verify_attestation(store: Any, att: Any, subject_did: str) -> bool:
    """Full READ-time validation of one attestation for `subject_did`.
    Enforces: shape/protocol, claim, exact subject, allowlisted issuer,
    INDEPENDENCE (issuer is neither the Guild nor the subject), validity
    window, and the issuer's Ed25519 signature over the JCS body."""
    if not _valid_shape(att):
        return False
    if att["claim"] != CLAIM:
        return False
    if att["subject_did"] != subject_did:
        return False
    issuer = str(att["issuer_did"])
    if issuer not in allowlisted_issuers():
        return False
    # independence: self-attestation and Guild-attestation can never count,
    # even if such a DID is (mis)configured onto the allowlist.
    if issuer == subject_did:
        return False
    try:
        if issuer == store.guild_did():
            return False
    except Exception:
        return False
    # STRICT time validation on untrusted issuer timestamps: both bounds
    # must parse as timezone-aware RFC 3339 datetimes; the window must not
    # be inverted; a future-issued attestation (beyond small skew) is
    # invalid; the attestation must be currently inside its window.
    issued = _parse_rfc3339(att["issued_at"])
    expires = _parse_rfc3339(att["expires_at"])
    if issued is None or expires is None:
        return False
    if issued >= expires:
        return False                      # inverted/empty validity window
    now = datetime.now(timezone.utc)
    if (issued - now).total_seconds() > ISSUED_AT_SKEW_S:
        return False                      # issued in the future
    if not (issued <= now < expires):
        return False
    body = {k: v for k, v in att.items() if k != "proof"}
    try:
        return bool(crypto.verify_jcs(body, att["proof"],
                                      crypto.public_key_from_did(issuer)))
    except Exception:
        return False


def attestation_for(store: Any, subject_did: str) -> Optional[dict[str, Any]]:
    """The first currently-valid independent externality attestation for
    `subject_did`, or None. None is the NORMAL state: with the default empty
    allowlist this always returns None."""
    if not subject_did or not allowlisted_issuers():
        return None
    atts = getattr(store, "externality_attestations", None) or {}
    for att in atts.values():
        if verify_attestation(store, att, subject_did):
            return dict(att)
    return None


class AttestationError(ValueError):
    pass


def record_attestation(store: Any, att: Any) -> dict[str, Any]:
    """Persist one externality attestation (bounded store). Shape and
    signature are checked at write time; allowlist/independence/window are
    ALWAYS re-checked at read time, so storing a document grants nothing."""
    if not _valid_shape(att):
        raise AttestationError("malformed externality attestation")
    body = {k: v for k, v in att.items() if k != "proof"}
    try:
        ok = crypto.verify_jcs(body, att["proof"],
                               crypto.public_key_from_did(att["issuer_did"]))
    except Exception:
        ok = False
    if not ok:
        raise AttestationError("attestation signature invalid")
    return store.record_externality_attestation(att)
