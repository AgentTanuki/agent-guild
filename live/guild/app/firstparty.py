"""First-party authentication — the authoritative mechanism for classifying
Agent Guild's OWN traffic (scheduled jobs, canaries, probes, seed tooling) so
it is reliably excluded from external-growth metrics.

This is a CLASSIFICATION + internal-authentication mechanism, NOT a universal
admin credential. A valid first-party token:
  * tags the caller as first-party -> caller class AG_INTERNAL or AG_TEST;
  * NEVER grants agent scopes, billing authority, or admin authority (those
    require a real api key / the admin token, checked separately).

Header (dedicated, authenticated):   X-Agent-Guild-First-Party
Legacy header (accepted, migration): X-Guild-Source
Role header (optional):              X-Agent-Guild-Role: test | internal

Token config (env, never logged, never in git):
  GUILD_FIRST_PARTY_TOKEN        the current token
  GUILD_FIRST_PARTY_TOKEN_PREV   optional: the previous token, accepted ONLY
                                 during a rotation window, then removed.

Comparison is constant-time (hmac.compare_digest). The raw token is never
recorded — only whether first-party auth succeeded, the caller class, the
caller id, the route and a timestamp.
"""
from __future__ import annotations

import hmac
import os
from typing import Optional

HEADER = "X-Agent-Guild-First-Party"
LEGACY_HEADER = "X-Guild-Source"
ROLE_HEADER = "X-Agent-Guild-Role"


def _configured_tokens() -> list[str]:
    """Current + optional previous token (rotation window). Non-empty only."""
    return [t for t in (os.environ.get("GUILD_FIRST_PARTY_TOKEN", ""),
                        os.environ.get("GUILD_FIRST_PARTY_TOKEN_PREV", ""))
            if t]


def strict_mode() -> bool:
    """True once a token is configured: only an exact token match is first-party.
    Until then, honor mode is in effect (transitional; see is_first_party)."""
    return bool(_configured_tokens())


def is_first_party(presented: Optional[str], legacy: Optional[str] = None) -> bool:
    """True iff the caller presented a valid first-party token.

    - STRICT mode (a token is configured): the presented value (dedicated header
      preferred, legacy header accepted) must equal a configured token by
      CONSTANT-TIME comparison. An invalid or missing token is NOT first-party.
    - HONOR mode (no token configured — pre-activation default): any non-empty
      value tags first-party, preserving today's behaviour until the operator
      sets the token. Activation (setting the token) ends honor mode."""
    value = presented or legacy
    if not value:
        return False
    tokens = _configured_tokens()
    if not tokens:
        return True  # honor mode (transitional)
    return any(hmac.compare_digest(str(value), str(t)) for t in tokens)


def role_of(role_header: Optional[str]) -> str:
    """Explicit first-party role. Defaults to 'internal'. 'test' marks
    verification/test tooling so it classifies AG_TEST; anything else is an
    operational internal job -> AG_INTERNAL. The role is only honoured for a
    caller that already passed is_first_party()."""
    return "test" if (role_header or "").strip().lower() == "test" else "internal"
