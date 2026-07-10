"""L1 — AG Identity Factory.

Turns validated capability templates into versioned, Guild-signed AG identity
documents. Identities are DATA, not processes: the shared Pilot A runtime is
this FastAPI service. An identity is only built (and therefore only published)
if its capability's fixture suite passes — the publish gate. Signatures are
ed25519 over the JCS-canonical `identity` body, verifiable against
/.well-known/agent-guild-did.json (same key that signs Agent Passports).
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Optional

from ..crypto import sign_jcs
from .capabilities import CAPABILITIES, Capability, validate_all

SWARM_TAG = "swarm_identity"          # marks first-party swarm records everywhere


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ag_id_for(cap: Capability) -> str:
    """Stable, globally unique AG identifier for (capability, major version)."""
    major = cap.version.split(".", 1)[0]
    return "agid_" + sha256(f"{cap.id}@{major}".encode()).hexdigest()[:12]


def build_identity(cap: Capability, benchmark: dict, base: str,
                   guild_identity: dict, owner_agent_id: Optional[str]) -> dict:
    """One signed AG identity document. `benchmark` is the fixture-gate result."""
    body = {
        "schema_version": "ag-identity/1",
        "ag_id": ag_id_for(cap),
        "name": cap.name,                               # optional human metadata
        "capability": {
            "id": cap.id,
            "version": cap.version,
            "summary": cap.summary,
            "description": cap.description,
            "tags": list(cap.tags),
            "category": cap.id.split(".", 1)[0],
            "input_schema": cap.input_schema,
            "output_schema": cap.output_schema,
        },
        "protocols": {
            "rest": {"method": "POST", "url": f"{base}/invoke/{cap.id}",
                     "content_type": "application/json",
                     "body": "the input_schema object, directly"},
            "mcp": {"transport": "streamable-http", "url": f"{base}/mcp",
                    "tool": "ag_" + cap.id.replace(".", "_")},
            "a2a": {"url": f"{base}/a2a",
                    "message": f"invoke: {cap.id} <json payload>"},
        },
        "auth": {
            "guest": {"required": False,
                      "limits": "see /terms.json — inspect BEFORE invoking"},
            "member": {"header": "X-API-Key",
                       "how_to_join": f"{base}/terms.json"},
        },
        "pricing": {"guest_cost_credits": cap.est_cost_credits,
                    "member_cost_credits": cap.est_cost_credits,
                    "note": "free within rate limits in Pilot A"},
        "expected_latency_ms": cap.est_latency_ms,
        "reliability": {
            "fixture_pass_rate": (benchmark["passed"] / benchmark["total"])
            if benchmark["total"] else None,
            "deterministic": True,
        },
        "benchmark": {k: benchmark[k] for k in
                      ("total", "passed", "failed", "ok", "avg_latency_ms")},
        "context_limits": cap.context_limits,
        "known_failure_modes": list(cap.failure_modes),
        "prohibited_uses": list(cap.prohibited_uses),
        "safety_class": cap.safety_class,
        "owner": {"runtime": "agent-guild-shared-runtime/pilot-a",
                  "agent_id": owner_agent_id,
                  "operator": "Agent Guild (first-party; excluded from the "
                              "Guild's external growth metrics)"},
        "guild_membership": {
            "member_of": "Agent Guild",
            "guild_did": guild_identity["did"],
            "attestation": "identity signed by the Guild DID below",
            "verify_against": f"{base}/.well-known/agent-guild-did.json",
        },
        "provenance": {"envelope": "every completion returns a signed provenance "
                                   "envelope — see any invocation response",
                       "terms": f"{base}/terms.json"},
        "created_at": _now(),
        "updated_at": _now(),
        "health": "passing" if benchmark["ok"] else "failing",
    }
    signature = sign_jcs(body, guild_identity["private_key"])
    return {"identity": body,
            "signature": {"alg": "Ed25519", "over": "JCS(identity)",
                          "signature": signature,
                          "public_key": guild_identity["public_key"],
                          "signer_did": guild_identity["did"]}}


class IdentityRegistry:
    """Builds + caches the Pilot A identity set behind the publish gate."""

    def __init__(self):
        self._docs: dict[str, dict] = {}       # ag_id -> signed doc
        self._by_cap: dict[str, str] = {}      # capability id -> ag_id
        self._gate: dict[str, dict] = {}       # capability id -> fixture result
        self._built_at: Optional[str] = None

    def build(self, base: str, guild_identity: dict,
              owner_ids: Optional[dict[str, str]] = None) -> dict:
        """Run the publish gate and (re)build every passing identity document."""
        self._gate = validate_all()
        self._docs, self._by_cap = {}, {}
        for cap_id, cap in sorted(CAPABILITIES.items()):
            result = self._gate[cap_id]
            if not result["ok"]:
                continue                        # the gate: failing => unpublished
            doc = build_identity(cap, result, base, guild_identity,
                                 (owner_ids or {}).get(cap_id))
            aid = doc["identity"]["ag_id"]
            self._docs[aid] = doc
            self._by_cap[cap_id] = aid
        self._built_at = _now()
        return {"published": len(self._docs),
                "excluded": [c for c, r in self._gate.items() if not r["ok"]],
                "built_at": self._built_at}

    @property
    def built(self) -> bool:
        return self._built_at is not None

    def gate_results(self) -> dict:
        return self._gate

    def get(self, ag_id: str) -> Optional[dict]:
        return self._docs.get(ag_id)

    def for_capability(self, cap_id: str) -> Optional[dict]:
        aid = self._by_cap.get(cap_id)
        return self._docs.get(aid) if aid else None

    def index(self, base: str) -> dict:
        """The machine-discovery index served at
        /.well-known/ag-identities/index.json."""
        entries = []
        for aid, doc in sorted(self._docs.items()):
            ident = doc["identity"]
            entries.append({
                "ag_id": aid,
                "capability": ident["capability"]["id"],
                "version": ident["capability"]["version"],
                "summary": ident["capability"]["summary"],
                "tags": ident["capability"]["tags"],
                "invoke": ident["protocols"]["rest"]["url"],
                "mcp_tool": ident["protocols"]["mcp"]["tool"],
                "document": f"{base}/identities/{aid}",
                "guest_cost_credits": ident["pricing"]["guest_cost_credits"],
                "expected_latency_ms": ident["expected_latency_ms"],
                "health": ident["health"],
            })
        return {
            "schema_version": "ag-identity-index/1",
            "name": "Agent Guild — invocable capability identities",
            "description": ("Narrow, deterministic, fixture-verified capabilities "
                            "any external agent can invoke as a guest, free, with "
                            "signed provenance on every completion. Inspect "
                            "/terms.json before invoking. Join via "
                            "POST /agents/register when membership has positive "
                            "expected utility for you."),
            "count": len(entries),
            "built_at": self._built_at,
            "terms": f"{base}/terms.json",
            "provenance_verify": f"{base}/.well-known/agent-guild-did.json",
            "identities": entries,
        }


registry = IdentityRegistry()
