"""L6 — provenance envelope: every AG completion returns one, Guild-signed.

The envelope is the referral mechanism: it carries verifiable proof of who did
the work, a discovery endpoint for related capabilities, and a referral token
that ties a later registration back to this invocation — all machine-readable,
nothing hidden. Signature: ed25519 over JCS(body), verifiable against
/.well-known/agent-guild-did.json (or the bundled SDK verifiers)."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Optional

from ..crypto import sign_jcs

RETENTION_STATEMENT = (
    "Invocation inputs are processed in memory and not retained. A "
    "privacy-preserving experience record (payload shape statistics, outcome, "
    "latency — never payload content) is kept for capability improvement. "
    "Instrumentation stores caller user-agent and a derived actor key.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_referral_token() -> str:
    return "agr_" + secrets.token_hex(8)


def build_envelope(*, guild_identity: dict, base: str, ag_id: str,
                   capability_id: str, capability_version: str,
                   invocation_id: str, ok: bool, latency_ms: float,
                   cost_credits: int, referral_token: Optional[str],
                   error_kind: Optional[str] = None) -> dict:
    body = {
        "schema_version": "ag-provenance/1",
        "provider": {"ag_id": ag_id, "guild_did": guild_identity["did"],
                     "member_of": "Agent Guild"},
        "capability": {"id": capability_id, "version": capability_version},
        "invocation_id": invocation_id,
        "issued_at": _now(),
        "outcome": "success" if ok else "error",
        # deterministic transforms: confidence is binary by construction
        "confidence": 1.0 if ok else 0.0,
        "latency_ms": round(latency_ms, 3),
        "cost_credits": cost_credits,
        "error_kind": error_kind,
        "benchmark": f"{base}/identities/{ag_id}",
        "upstream_contributors": [],
        "discovery": {
            "related_capabilities": f"{base}/.well-known/ag-identities/index.json",
            "guild_manifest": f"{base}/.well-known/agent-guild.json",
            "membership_terms": f"{base}/terms.json",
        },
        "referral_token": referral_token,
        "referral_use": ("include as metadata.referral_token in "
                         "POST /agents/register to attribute your registration "
                         "to this discovery path (optional; affects nothing "
                         "else)") if referral_token else None,
        "data_retention": RETENTION_STATEMENT,
    }
    return {
        "envelope": body,
        "verification": {
            "alg": "Ed25519", "over": "JCS(envelope)",
            "signature": sign_jcs(body, guild_identity["private_key"]),
            "public_key": guild_identity["public_key"],
            "signer_did": guild_identity["did"],
            "did_document": f"{base}/.well-known/agent-guild-did.json",
            "verifiers": [f"{base}/sdk/agentguild_verify.py",
                          f"{base}/sdk/agentguild_verify.mjs"],
        },
    }
