"""The public trust index — what is out there, and which of it actually works.

THE PRODUCT QUESTION
  "Can I safely use or pay this specific endpoint right now?"

Everything here serves that question. The index is not a directory competing on
inventory size; a bigger list of unverified entries is a worse product, not a
better one. The index earns its place by carrying, per endpoint, the one thing
every registry omits: **what happened when we actually called it.**

THE DISTINCTION THAT MAKES IT HONEST
  Registries conflate three completely different states and report them all as
  a listing. This module keeps them apart and never lets one be read as
  another:

    ``indexed``       a source listed it. We have never called it. This is a
                      CLAIM, and is worth exactly what a claim is worth.
    ``live``          it completed a real protocol handshake when we called it.
    ``unreachable``   we called it and it did not answer.
    ``degraded``      it answers, but one of its own declared claims does not
                      hold (unsigned card, advertises payment but never
                      challenges, and so on).

  Measured on 2026-07-31: 92.9% of a2aregistry entries report ``is_healthy:
  true`` and 33.9% complete a task. That 59-point gap is the entire reason this
  module exists, and it is why ``indexed`` is never promoted to ``live``
  without an observation of our own.

DEDUPLICATION — and its exact limits
  Two levels, both DETERMINISTIC:

    1. ENDPOINT — normalised scheme+host+port+path. One service listed by three
       registries is one entry with three provenance records.
    2. DECLARED IDENTITY (DID) — when two endpoints declare the SAME did:key,
       they are one subject with several addresses. The first becomes the
       canonical entry; the others become `alias_endpoints` on it, keeping
       their own provenance and their own last observation.

  OPERATOR identity is deliberately NOT inferred. Two endpoints with similar
  names, a shared domain or the same contact string are NOT evidence of one
  operator, and guessing would quietly merge unrelated parties — the opposite
  of the error this index exists to prevent, and a worse one, because a merged
  entry launders one party's evidence into another's. Operator remains
  `unknown` unless a deterministic declared identifier says otherwise.

PROVENANCE AND FRESHNESS
  Every entry records where it came from, when each source last confirmed it,
  and when WE last observed it. An observation has an age, and an aged
  observation is reported with its age rather than being quietly served as
  current. `stale` is a first-class state, not an absence.

WHAT IS DELIBERATELY NOT HERE
  No scores invented from unknowns, no aggregate "trust rating" that averages
  an observation with a guess, and no generated pages for entries we have
  nothing real to say about — an evidence page for an endpoint we have never
  successfully called is SEO spam, and is refused by `is_page_worthy`.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

from . import reachability

#: An observation older than this is reported as stale rather than current.
DEFAULT_FRESH_TTL_S = 24 * 3600

#: Bound on how much of the index one recheck cycle may probe, so an autonomous
#: loop cannot turn into an unbounded crawler of other people's infrastructure.
DEFAULT_RECHECK_BATCH = 8

STATUS_INDEXED = "indexed"
STATUS_LIVE = "live"
STATUS_DEGRADED = "degraded"
STATUS_UNREACHABLE = "unreachable"

#: Ownership classes. `externally_owned` is the ONLY one that may appear in a
#: growth or revenue metric.
OWNER_EXTERNAL = "externally_owned"
OWNER_FIRST_PARTY = "first_party"
OWNER_UNKNOWN = "unknown"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).isoformat()


def fresh_ttl_s() -> int:
    try:
        return max(300, min(int(os.environ.get("GUILD_INDEX_FRESH_TTL_S")
                                or DEFAULT_FRESH_TTL_S), 30 * 24 * 3600))
    except (TypeError, ValueError):
        return DEFAULT_FRESH_TTL_S


def recheck_batch() -> int:
    try:
        return max(1, min(int(os.environ.get("GUILD_INDEX_RECHECK_BATCH")
                              or DEFAULT_RECHECK_BATCH), 50))
    except (TypeError, ValueError):
        return DEFAULT_RECHECK_BATCH


def normalise_url(url: str) -> str:
    """Canonical form for deduplication.

    Lowercase scheme+host, drop the default port, drop a trailing slash, drop
    query and fragment. Two registries listing ``https://X.com/a2a`` and
    ``https://X.com:443/a2a/`` are describing ONE endpoint, and counting them
    twice would inflate the only number this index is judged on."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = (parts.path or "").rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def fingerprint(url: str) -> str:
    """Stable public id for an endpoint. Derived from the normalised URL only —
    it carries no secret and can be published, linked and cited."""
    norm = normalise_url(url)
    if not norm:
        return ""
    return "ep_" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def new_entry(url: str, source: str, *, declared: Optional[dict] = None,
              did: str = "") -> dict[str, Any]:
    norm = normalise_url(url)
    return {
        "id": fingerprint(norm),
        "endpoint": norm,
        "did": did or "",
        "declared": declared or {},          # what the SOURCE claims
        "sources": [{"source": source, "first_seen": _iso(),
                     "last_seen": _iso()}],
        "first_indexed_at": _iso(),
        "status": STATUS_INDEXED,
        "owner_class": OWNER_UNKNOWN,
        "observation": None,                  # what WE saw, or None
        "observed_at": None,
        "observation_count": 0,
        "drift": [],                          # declared-vs-observed changes
        # Other endpoints that declare the SAME did. Each keeps its own
        # provenance and observation; they are addresses of one subject, not
        # separate subjects, and are never counted separately in inventory.
        "alias_endpoints": [],
    }


def merge_source(entry: dict[str, Any], source: str) -> dict[str, Any]:
    """Record that `source` also lists this endpoint (provenance, not a count)."""
    for rec in entry.get("sources", []):
        if rec.get("source") == source:
            rec["last_seen"] = _iso()
            return entry
    entry.setdefault("sources", []).append(
        {"source": source, "first_seen": _iso(), "last_seen": _iso()})
    return entry


def merge_alias(canonical: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """Fold `other` into `canonical` as an ALIAS ENDPOINT of the same subject.

    Nothing is discarded: the alias keeps its endpoint, its sources and its own
    last observation, and every source of the alias is also recorded on the
    canonical entry so provenance survives the merge. Idempotent — folding the
    same alias twice updates it rather than adding a duplicate."""
    aliases = canonical.setdefault("alias_endpoints", [])
    payload = {
        "endpoint": other.get("endpoint"),
        "id": other.get("id"),
        "sources": other.get("sources", []),
        "status": other.get("status"),
        "observed_at": other.get("observed_at"),
        "merged_at": _iso(),
        "merged_on": "declared_did",
    }
    for i, existing in enumerate(aliases):
        if existing.get("endpoint") == payload["endpoint"]:
            aliases[i] = payload
            break
    else:
        aliases.append(payload)
    for src in other.get("sources", []):
        merge_source(canonical, src.get("source", "unknown"))
    if not canonical.get("declared") and other.get("declared"):
        canonical["declared"] = other["declared"]
    return canonical


def is_stale(entry: dict[str, Any], ttl_s: Optional[int] = None) -> bool:
    """Has our own observation aged out? An entry never observed is stale."""
    at = entry.get("observed_at")
    if not at:
        return True
    try:
        seen = datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return True
    return _now() - seen > timedelta(seconds=ttl_s or fresh_ttl_s())


def observation_age_s(entry: dict[str, Any]) -> Optional[float]:
    at = entry.get("observed_at")
    if not at:
        return None
    try:
        return round((_now() - datetime.fromisoformat(at)).total_seconds(), 1)
    except (TypeError, ValueError):
        return None


def status_from_preflight(result: dict[str, Any]) -> str:
    """Map a preflight verdict onto an index status.

    `indexed` is NEVER produced here: reaching this function means we called
    the endpoint, and the whole point of the distinction is that a listing and
    an observation are different things."""
    verdict = result.get("verdict")
    checks = {c["check"]: c["status"] for c in result.get("checks", [])}
    if checks.get("endpoint_reachable") == "failed":
        return STATUS_UNREACHABLE
    if checks.get("protocol_handshake") != "proven":
        # answered, but proved no agent protocol — the 92.9%/33.9% gap
        return STATUS_DEGRADED
    if verdict == "delegate_with_caution":
        return STATUS_DEGRADED
    return STATUS_LIVE


def apply_observation(entry: dict[str, Any], result: dict[str, Any],
                      *, owner_class: str = OWNER_UNKNOWN) -> dict[str, Any]:
    """Fold a preflight result into an entry, recording DRIFT.

    Drift is the property a one-off review cannot have: a server that passed
    once and changed afterwards. Every status transition is retained with its
    timestamp, bounded so the record cannot grow without limit."""
    previous = entry.get("status")
    status = status_from_preflight(result)
    entry["status"] = status
    entry["observation"] = {
        "verdict": result.get("verdict"),
        "checks": result.get("checks", []),
        "failed": result.get("failed", []),
        "unknowns": result.get("unknowns", []),
    }
    entry["observed_at"] = _iso()
    entry["observation_count"] = int(entry.get("observation_count", 0)) + 1
    if owner_class != OWNER_UNKNOWN or not entry.get("owner_class"):
        entry["owner_class"] = owner_class
    if previous and previous != status:
        entry.setdefault("drift", []).append(
            {"at": _iso(), "from": previous, "to": status})
        entry["drift"] = entry["drift"][-20:]
    return entry


def is_page_worthy(entry: dict[str, Any]) -> bool:
    """May we publish an indexable evidence page for this entry?

    ONLY when we hold a real observation of our own. A page for an endpoint we
    have merely seen listed contains nothing a reader cannot get from the
    registry, and publishing it at scale is SEO spam. The mandate says the
    index must be evidence-rich and unique or it must not exist, and this is
    the function that enforces it."""
    if not entry.get("observation") or not entry.get("observed_at"):
        return False
    if entry.get("status") == STATUS_INDEXED:
        return False
    obs = entry.get("observation") or {}
    # something concrete must have been established either way
    return bool(obs.get("checks"))


def public_view(entry: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    """The free public projection. Never invents a score.

    `claimed` and `observed` are kept in separate blocks on purpose: the entire
    failure mode this index exists to correct is a reader taking a claim for an
    observation because a UI merged them."""
    age = observation_age_s(entry)
    out: dict[str, Any] = {
        "id": entry.get("id"),
        "endpoint": entry.get("endpoint"),
        "status": entry.get("status"),
        "owner_class": entry.get("owner_class", OWNER_UNKNOWN),
        "claimed": {
            "name": (entry.get("declared") or {}).get("name"),
            "capabilities": (entry.get("declared") or {}).get("capabilities", []),
            "sources": [s.get("source") for s in entry.get("sources", [])],
            "note": "self-declared by the endpoint or its registry — NOT verified",
        },
        "observed": None,
        "observation_age_seconds": age,
        "stale": is_stale(entry),
        "observation_count": entry.get("observation_count", 0),
        "first_indexed_at": entry.get("first_indexed_at"),
        "alias_endpoints": [a.get("endpoint")
                            for a in entry.get("alias_endpoints", [])],
        "identity": {
            "did": entry.get("did") or None,
            "dedupe": ("endpoint + declared DID" if entry.get("did")
                       else "endpoint only (no declared identity)"),
            "operator": "unknown — operator identity is never inferred from "
                        "names, domains or contact strings",
        },
    }
    if entry.get("observation"):
        obs = entry["observation"]
        out["observed"] = {
            "verdict": obs.get("verdict"),
            "failed": obs.get("failed", []),
            "unknowns": obs.get("unknowns", []),
            "at": entry.get("observed_at"),
            "note": ("what happened when the Guild actually called this "
                     "endpoint. `unknowns` are checks we could not perform and "
                     "are excluded from the verdict, never averaged into it."),
        }
        if detail:
            out["observed"]["checks"] = obs.get("checks", [])
            out["drift"] = entry.get("drift", [])
            out["sources"] = entry.get("sources", [])
    else:
        out["observed_note"] = (
            "NEVER CALLED BY THE GUILD. This entry is a listing, not an "
            "observation — treat it as a claim. Run GET /preflight?url=… to "
            "produce one now (free).")
    return out


def summarise(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Index-level counts, split so no number can be quoted out of context."""
    rows = list(entries)
    by_status: dict[str, int] = {}
    by_owner: dict[str, int] = {}
    observed = 0
    for e in rows:
        by_status[e.get("status", STATUS_INDEXED)] = \
            by_status.get(e.get("status", STATUS_INDEXED), 0) + 1
        by_owner[e.get("owner_class", OWNER_UNKNOWN)] = \
            by_owner.get(e.get("owner_class", OWNER_UNKNOWN), 0) + 1
        if e.get("observation"):
            observed += 1
    total = len(rows)
    listed_only = by_status.get(STATUS_INDEXED, 0)
    aliased = sum(len(e.get("alias_endpoints") or []) for e in rows)
    return {
        "total_entries": total,
        "alias_endpoints_folded": aliased,
        "distinct_endpoints_known": total + aliased,
        "observed_by_guild": observed,
        "never_called_by_guild": listed_only,
        "by_status": by_status,
        "by_owner_class": by_owner,
        "claim_vs_observation": (
            f"{observed} of {total} entries carry an observation of our own; "
            f"{listed_only} are listings we have never called. A listing is a "
            "claim. Inventory size is a supporting metric and is never "
            "reported as adoption."),
        "dedupe": (
            f"{total} subjects across {total + aliased} known endpoints "
            f"({aliased} folded as aliases of the same declared DID). "
            "Deduplication is endpoint-level and declared-DID-level only; "
            "operator identity is NEVER inferred from names or domains, so two "
            "entries may belong to one operator and we will not claim they do."),
    }
