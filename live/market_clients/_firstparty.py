"""SHARED first-party authentication for every Agent Guild-OPERATED script
(corrective pass 2026-07-13).

Every script in live/market_clients (and any other Guild-run tooling) is
first-party traffic by definition: it must authenticate as such on EVERY
request so it can never pollute genuine-external or economic metrics.

Behaviour:
  * strict mode (GUILD_FIRST_PARTY_TOKEN set in the env or in
    live/secrets/first_party_token): send the real token;
  * honor mode (no token configured server-side yet): send a non-empty
    sentinel — the server tags any non-empty value first-party until the
    operator activates strict mode.

Either way the headers are ALWAYS present. The old per-script copies
returned {} when no token existed, which silently classified Guild-operated
runs as external — the exact attribution failure this helper closes.
"""
from __future__ import annotations

import os
import pathlib

_TOKEN_FILE = pathlib.Path(__file__).resolve().parent.parent / "secrets" / \
    "first_party_token"

HEADER = "X-Agent-Guild-First-Party"
ROLE_HEADER = "X-Agent-Guild-Role"
HONOR_MODE_SENTINEL = "guild-operated-script"


def first_party_token() -> str:
    tok = os.environ.get("GUILD_FIRST_PARTY_TOKEN", "").strip()
    if not tok and _TOKEN_FILE.exists():
        tok = _TOKEN_FILE.read_text().strip()
    return tok


def firstparty_headers(role: str = "test") -> dict[str, str]:
    """Headers EVERY Guild-operated request must carry. Never empty."""
    return {
        HEADER: first_party_token() or HONOR_MODE_SENTINEL,
        ROLE_HEADER: role if role in ("test", "internal") else "test",
    }
