"""Content references for the filtered fraud watchlist."""
import hashlib
import re

from .crypto import canonicalize_jcs


def snapshot(flagged: list, min_suspicion: float) -> dict:
    # Carry the threshold text so clients need no float formatting rule.
    payload = {"min_suspicion": str(min_suspicion or 0.0), "flagged": flagged}
    digest = hashlib.sha256(canonicalize_jcs(payload).encode("utf-8")).hexdigest()
    return {**payload, "count": len(flagged), "watchlist_id": "wl1_" + digest,
            "citation": {"optional": True,
                         "verify": "wl1_ + lowercase hex SHA-256 of UTF-8 AGI-1 "
                                   "canonical JSON of {min_suspicion, flagged} as returned",
                         "report": "Include watchlist_id and optional watchlist_effect "
                                   "(changed, confirmed, none, unknown) in "
                                   "POST /preflight/outcome or in the requester-signed "
                                   "core of POST /outcomes."}}


def citation_fields(body) -> dict:
    version = body.get("watchlist_id")
    effect = body.get("watchlist_effect")
    if version is None and effect is None:
        return {}
    if not isinstance(version, str) or not re.fullmatch(r"wl1_[0-9a-f]{64}", version):
        raise ValueError("watchlist_id must be a wl1_ SHA-256 reference")
    if effect is not None and effect not in ("changed", "confirmed", "none", "unknown"):
        raise ValueError("watchlist_effect must be changed, confirmed, none or unknown")
    return {"watchlist_id": version, "watchlist_effect": effect or "unknown"}
