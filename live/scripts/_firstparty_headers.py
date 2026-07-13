"""Shared helper: first-party classification headers for Guild tooling.

Reads the strict first-party token from GUILD_FIRST_PARTY_TOKEN or
live/secrets/first_party_token and returns the headers every first-party
script must send so our own traffic classifies AG_INTERNAL/AG_TEST under
strict attribution (app/firstparty.py).

CORRECTIVE 2026-07-13: the headers are NEVER empty. Under honor mode (no
token configured server-side) any non-empty value tags first-party, so a
sentinel is sent — the old {} return silently classified Guild-operated
scripts as external traffic.
"""
import os
import pathlib

HONOR_MODE_SENTINEL = "guild-operated-script"


def first_party_headers(role: str = "internal") -> dict:
    tok = os.environ.get("GUILD_FIRST_PARTY_TOKEN", "").strip()
    if not tok:
        p = pathlib.Path(__file__).resolve().parents[1] / "secrets" / "first_party_token"
        if p.exists():
            tok = p.read_text().strip()
    return {"X-Agent-Guild-First-Party": tok or HONOR_MODE_SENTINEL,
            "X-Agent-Guild-Role": role}
