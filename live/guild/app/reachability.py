"""Reachability semantics — the single source of truth for how the Guild
talks about whether a provider can actually be contacted.

Formal definitions (entry, expiry, verification method, safe inference,
recommend/route policy) live in docs/discovery-swarm/REACHABILITY_SEMANTICS.md
and are enforced here so every surface (/check, /search shortlists, A2A, MCP)
emits identical, honest fields.

Status ladder (only the first three are producible today):

  no_endpoint          — the agent has never declared an endpoint.
  declared_unverified  — an endpoint URL is on file. It is a CLAIM by the
                         agent, verified by nobody. Never described as
                         "reachable".
  unknown              — an endpoint string is on file but malformed
                         (not http(s), unparseable) — worse than absent.
  recently_reachable   — reserved: a declaration-time liveness check
                         succeeded within the last 24 h. Requires the
                         SSRF-safe verifier (owner-initiated, at declaration
                         only, scheme+host validated, no redirects, private
                         address space blocked, single request, short
                         timeout). NOT IMPLEMENTED — never produced.
  currently_unreachable— reserved: the last verification attempt failed
                         (expires back to declared_unverified after 24 h).
  invocation_verified  — reserved: a guild-observed task receipt travelled
                         through this endpoint within the last 7 days. The
                         only status that proves the endpoint does WORK.

Policy encoded here:
  * recommend-with-disclosure is allowed from declared_unverified upward —
    the status field itself is the disclosure;
  * recommended_for_routing (the Guild actively sending work) requires
    recently_reachable or invocation_verified — impossible today, so the
    field is honestly False everywhere until the verifier ships.
"""
from __future__ import annotations

from typing import Any, Optional

PRODUCIBLE_STATUSES = ("no_endpoint", "declared_unverified", "unknown")
RESERVED_STATUSES = ("recently_reachable", "currently_unreachable",
                     "invocation_verified")
ROUTABLE_STATUSES = ("recently_reachable", "invocation_verified")


def status_for(endpoint: Optional[str]) -> str:
    if not endpoint:
        return "no_endpoint"
    if not (str(endpoint).startswith("http://")
            or str(endpoint).startswith("https://")):
        return "unknown"
    return "declared_unverified"


def reachability_fields(endpoint: Optional[str]) -> dict[str, Any]:
    """The full honest field set for one provider. last_verified_at /
    verification_age_seconds are None and verification_method is
    'declaration_only' (or None without an endpoint) until the SSRF-safe
    verifier exists — no field may ever imply a check that never ran."""
    status = status_for(endpoint)
    declared = status == "declared_unverified"
    return {
        "has_declared_endpoint": declared,
        "reachability_status": status,
        "verification_method": "declaration_only" if declared else None,
        "last_verified_at": None,
        "verification_age_seconds": None,
        # True only when the endpoint has been PROVEN to accept an invocation
        # (invocation_verified) — a declaration proves nothing.
        "invocation_supported": False,
        # The Guild routes work only through verified routes; none exist yet.
        "recommended_for_routing": status in ROUTABLE_STATUSES,
    }
