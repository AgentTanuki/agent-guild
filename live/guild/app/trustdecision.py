"""Strict marketplace input for paid, caller-bound AGD-1 decisions."""
from __future__ import annotations

import re
from typing import Any

from .crypto import canonicalize_jcs
from .x402_artifacts import sha256_hex

MAX_CAPABILITY = 200
MIN_TTL_S = 60
MAX_TTL_S = 7 * 86400
_PAYAN_URL = re.compile(
    r"^https://payanagent\.com/x402/[A-Za-z0-9_-]{8,128}$")


class TrustDecisionRefused(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


def normalise_request(body: Any) -> dict[str, Any]:
    """Return the exact, closed semantic input or fail closed."""
    if not isinstance(body, dict):
        raise TrustDecisionRefused("invalid_request", "request must be an object")
    allowed = {"capability", "ttl_seconds", "x402_resource_url"}
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise TrustDecisionRefused(
            "unknown_fields", "unsupported fields: " + ", ".join(unknown))
    capability = body.get("capability")
    if not isinstance(capability, str) or not capability.strip():
        raise TrustDecisionRefused(
            "invalid_capability", "capability must be a non-empty string")
    capability = capability.strip()
    if len(capability) > MAX_CAPABILITY:
        raise TrustDecisionRefused(
            "invalid_capability", f"capability must be <= {MAX_CAPABILITY} chars")
    ttl = body.get("ttl_seconds", 3600)
    if isinstance(ttl, bool) or not isinstance(ttl, int):
        raise TrustDecisionRefused(
            "invalid_ttl_seconds", "ttl_seconds must be an integer")
    if ttl < MIN_TTL_S or ttl > MAX_TTL_S:
        raise TrustDecisionRefused(
            "invalid_ttl_seconds",
            f"ttl_seconds must be {MIN_TTL_S}..{MAX_TTL_S}")
    out: dict[str, Any] = {"capability": capability, "ttl_seconds": ttl}
    relay = body.get("x402_resource_url")
    if relay is not None:
        if not isinstance(relay, str) or _PAYAN_URL.fullmatch(relay) is None:
            raise TrustDecisionRefused(
                "invalid_x402_resource_url",
                "x402_resource_url must be the exact canonical "
                "https://payanagent.com/x402/<offer-id> URL")
        out["x402_resource_url"] = relay
    return out


def request_sha256(body: Any, caller_did: str) -> str:
    """Opaque settlement binding over exact semantics and authenticated caller."""
    normalized = normalise_request(body)
    if not isinstance(caller_did, str) or not caller_did:
        raise TrustDecisionRefused(
            "verified_caller_required", "an authenticated caller DID is required")
    return sha256_hex(canonicalize_jcs({
        "request": normalized,
        "caller_did": caller_did,
    }).encode("utf-8"))
