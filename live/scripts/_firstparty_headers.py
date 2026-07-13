"""Shared helper: first-party classification headers for Guild tooling.

Reads the strict first-party token from GUILD_FIRST_PARTY_TOKEN or
live/secrets/first_party_token and returns the headers every first-party
script must send so our own traffic classifies AG_INTERNAL/AG_TEST under
strict attribution (app/firstparty.py). Returns {} when no token exists
(honor mode) — scripts keep working either way.
"""
import os
import pathlib


def first_party_headers(role: str = "internal") -> dict:
    tok = os.environ.get("GUILD_FIRST_PARTY_TOKEN", "").strip()
    if not tok:
        p = pathlib.Path(__file__).resolve().parents[1] / "secrets" / "first_party_token"
        if p.exists():
            tok = p.read_text().strip()
    if not tok:
        return {}
    return {"X-Agent-Guild-First-Party": tok, "X-Agent-Guild-Role": role}
