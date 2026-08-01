"""Index operations: ingest, recheck, watch — the autonomous half of the index.

Kept out of ``store.py`` deliberately. These are the only functions in the
service that reach out to third-party infrastructure on a schedule, so they are
in one file where their bounds can be read in a single sitting:

  * remote ingest is bounded default-ON, and ONLY for the cleared sources in
    ``indexsources.CLEARED_SOURCES`` (currently the documented, public,
    read-only MCP Registry API). ``GUILD_INDEX_INGEST=0`` remains the
    one-config-change kill switch, with no deploy — the property that matters
    when the traffic lands on someone else's servers. Sources we will not
    ingest are named with their exact gate in
    ``indexsources.UNAVAILABLE_SOURCES``, not silently omitted;
  * recheck probes at most ``recheck_batch()`` endpoints per cycle, oldest
    observation first, so the loop degrades to slow rather than to abusive;
  * a watch is charged per cycle ACTUALLY performed, so a dormant endpoint bills
    nothing and we can never invoice for work we did not do.

Everything writes through the same Store lock/transaction discipline as the
rest of the service, so an index write cannot half-land.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from . import indexsources, preflight, trustindex


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso() -> str:
    return _now().isoformat()


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------
def ingest(store: Any, records: Optional[list[dict[str, Any]]] = None
           ) -> dict[str, Any]:
    """Fold source records into the index. Deduplicates; never double-counts.

    A record that resolves to an endpoint already present adds PROVENANCE (a
    second source saw it) and nothing else. Inventory is not a success metric,
    so an ingest run that adds zero new endpoints is a perfectly good run."""
    if records is None:
        records = indexsources.collect(store)
    added = updated = skipped = aliased = 0
    with store.lock, store._txn():
        for rec in records:
            url = (rec.get("endpoint") or "").strip()
            norm = trustindex.normalise_url(url)
            if not norm:
                skipped += 1
                continue
            fp = trustindex.fingerprint(norm)
            entry = store.trust_index.get(fp)
            did = (rec.get("did") or "").strip()
            # DECLARED-DID COALESCING. Two endpoints declaring the same did:key
            # are one subject with two addresses. Fold the newcomer in as an
            # alias rather than creating a second entry, which would inflate
            # inventory — the one number this index is judged on.
            if entry is None and did:
                canonical = _by_did(store, did)
                if canonical is not None and canonical.get("endpoint") != norm:
                    provisional = trustindex.new_entry(
                        norm, rec.get("source", "unknown"),
                        declared=rec.get("declared"), did=did)
                    trustindex.merge_alias(canonical, provisional)
                    store.trust_index[canonical["id"]] = canonical
                    aliased += 1
                    continue
            if entry is None:
                entry = trustindex.new_entry(
                    norm, rec.get("source", "unknown"),
                    declared=rec.get("declared"), did=rec.get("did", ""))
                # Ownership from a DETERMINISTIC control only (the admin-gated
                # first_party flag), never inferred from a name or User-Agent.
                if rec.get("first_party"):
                    entry["owner_class"] = trustindex.OWNER_FIRST_PARTY
                store.trust_index[fp] = entry
                added += 1
            else:
                trustindex.merge_source(entry, rec.get("source", "unknown"))
                if rec.get("declared"):
                    entry["declared"] = {**(entry.get("declared") or {}),
                                         **rec["declared"]}
                if rec.get("first_party"):
                    entry["owner_class"] = trustindex.OWNER_FIRST_PARTY
                updated += 1
        if store.backend is not None:
            store._persist_kv("trust_index", store.trust_index)
        store._save()
    return {"added": added, "provenance_updated": updated,
            "aliased_to_existing_did": aliased,
            "skipped_unusable": skipped,
            "total_entries": len(store.trust_index),
            "note": ("added = SUBJECTS not previously known. "
                     "provenance_updated = already known, another source saw "
                     "it — NOT a new endpoint. aliased_to_existing_did = a new "
                     "endpoint folded into an existing subject because it "
                     "declares the same DID; it is an address, not a subject. "
                     "Inventory is a supporting metric and is never reported "
                     "as adoption.")}


def _by_did(store: Any, did: str) -> Optional[dict[str, Any]]:
    """The canonical entry declaring `did`, if any. Deterministic lookup only —
    no fuzzy matching, ever."""
    if not did:
        return None
    for entry in store.trust_index.values():
        if (entry.get("did") or "") == did:
            return entry
    return None


def reconcile_identities(store: Any) -> dict[str, Any]:
    """MIGRATION: fold pre-existing duplicates that share a declared DID.

    The index shipped keyed on endpoint alone, so two endpoints of one subject
    are already stored as two entries. This coalesces them once, deterministic-
    ally and idempotently: the OLDEST entry (by first_indexed_at, then by id
    for stability) wins as canonical, the rest become aliases with their
    provenance and last observation intact. Safe to run on every cycle — with
    nothing to merge it is a no-op."""
    by_did: dict[str, list] = {}
    for entry in store.trust_index.values():
        did = (entry.get("did") or "").strip()
        if did:
            by_did.setdefault(did, []).append(entry)
    merged = 0
    with store.lock, store._txn():
        for did, entries in by_did.items():
            if len(entries) < 2:
                continue
            entries.sort(key=lambda e: (e.get("first_indexed_at") or "",
                                        e.get("id") or ""))
            canonical, rest = entries[0], entries[1:]
            for other in rest:
                trustindex.merge_alias(canonical, other)
                store.trust_index.pop(other["id"], None)
                merged += 1
            store.trust_index[canonical["id"]] = canonical
        if merged and store.backend is not None:
            store._persist_kv("trust_index", store.trust_index)
        if merged:
            store._save()
    return {"merged_into_canonical": merged,
            "subjects": len(store.trust_index),
            "rule": ("same DECLARED did:key only. Operator equivalence is "
                     "never inferred from names, domains or contact strings.")}


def _owner_class_for(store: Any, entry: dict[str, Any]) -> str:
    """Ownership from deterministic signals only.

    An endpoint is first-party if it belongs to an agent we have explicitly
    marked first-party through the admin control. It is externally owned only
    if it is a live, observed endpoint that is NOT ours. Everything else stays
    `unknown` — which is the whole point: unknown is never promoted to
    external to make a number look better."""
    if entry.get("owner_class") == trustindex.OWNER_FIRST_PARTY:
        return trustindex.OWNER_FIRST_PARTY
    did = entry.get("did") or ""
    if did:
        try:
            agent = store.agent_by_did(did)
        except Exception:  # noqa: BLE001
            agent = None
        if agent and agent.get("first_party"):
            return trustindex.OWNER_FIRST_PARTY
        if agent:
            return trustindex.OWNER_EXTERNAL
    if entry.get("observation"):
        return trustindex.OWNER_EXTERNAL
    return trustindex.OWNER_UNKNOWN


def recheck_one(store: Any, fingerprint: str, *,
                runner: Optional[Callable] = None) -> Optional[dict[str, Any]]:
    """Observe one endpoint and fold the result in. Returns the public view."""
    entry = store.trust_index.get(fingerprint)
    if entry is None:
        return None
    probe = runner or (lambda url: preflight.run(url, store=store))
    result = probe(entry["endpoint"])
    with store.lock, store._txn():
        entry = store.trust_index.get(fingerprint) or entry
        trustindex.apply_observation(
            entry, result, owner_class=_owner_class_for(store, entry))
        store.trust_index[fingerprint] = entry
        if store.backend is not None:
            store._persist_kv("trust_index", store.trust_index)
        store._save()
    # STRUCTURAL first-party: this observation is produced by our own index
    # observer, not by an inbound caller. `first_party=True` alone was a bare
    # metadata field that caller_class never read.
    store.record_internal_event("index_observation", "index_observer",
                                endpoint="index",
                                target=entry["endpoint"],
                                status=entry["status"],
                                first_party=True)
    return trustindex.public_view(entry, detail=True)


def recheck_due(store: Any, *, limit: Optional[int] = None,
                runner: Optional[Callable] = None) -> dict[str, Any]:
    """Recheck the stalest entries, bounded.

    Oldest observation first so a large index degrades to SLOW coverage rather
    than to an unbounded burst against other people's servers."""
    cap = limit or trustindex.recheck_batch()
    due = [e for e in store.trust_index.values() if trustindex.is_stale(e)]
    due.sort(key=lambda e: (e.get("observed_at") or ""))
    checked = 0
    transitions: list[dict[str, Any]] = []
    for entry in due[:cap]:
        before = entry.get("status")
        view = recheck_one(store, entry["id"], runner=runner)
        checked += 1
        if view and view.get("status") != before:
            transitions.append({"id": entry["id"],
                                "endpoint": entry["endpoint"],
                                "from": before, "to": view.get("status")})
    return {"checked": checked, "due": len(due), "capped_at": cap,
            "transitions": transitions}


# --------------------------------------------------------------------------
# Continuous watch (self-provisioned — no staff, no onboarding)
# --------------------------------------------------------------------------
def watch_id(owner_key: str, endpoint: str) -> str:
    """Deterministic id, so provisioning the SAME watch twice is idempotent
    rather than creating a duplicate subscription that bills twice."""
    raw = f"{owner_key}|{trustindex.normalise_url(endpoint)}".encode("utf-8")
    return "wch_" + hashlib.sha256(raw).hexdigest()[:16]


class UnbillableWatch(ValueError):
    """The presented credential does not resolve to an account we can charge."""


def provision_watch(store: Any, presented_key: str, endpoint: str, *,
                    interval_s: int = 3600) -> dict[str, Any]:
    """Create (or return) a continuous watch. Idempotent by (account, endpoint).

    AUTHENTICATE BEFORE DOING WORK (correction 2026-07-31). This previously
    accepted any string as an owner key and provisioned outbound work before
    checking whether it could ever be billed: an unauthenticated caller could
    schedule recurring probes against third-party infrastructure at our
    expense, and a legitimate caller presenting a hashed `sk_` secret got a
    watch keyed on the raw secret, which then failed to charge and suspended
    itself on the first cycle.

    Both are fixed by resolving the presented credential to its ACCOUNT KEY —
    the same resolution `Store.charge` performs — and refusing if it does not
    resolve. What we persist is that resolved account key, never the raw
    secret, so a durable record can never leak a credential.

    Provisioning is free; charging before any observation exists would be
    charging for a promise."""
    norm = trustindex.normalise_url(endpoint)
    if not norm:
        raise ValueError("unusable endpoint url")
    owner_key = store._account_key(presented_key)
    if not owner_key or owner_key not in getattr(store, "accounts", {}):
        raise UnbillableWatch(
            "the presented credential does not resolve to a billable account; "
            "no outbound work is scheduled for a caller we cannot charge")
    wid = watch_id(owner_key, norm)
    interval = max(300, min(int(interval_s or 3600), 7 * 24 * 3600))
    with store.lock, store._txn():
        existing = store.watches.get(wid)
        if existing:
            existing["interval_s"] = interval
            existing["active"] = True
            store.watches[wid] = existing
            if store.backend is not None:
                store._persist_kv("watches", store.watches)
            store._save()
            return {**existing, "created": False,
                    "note": "existing watch returned — provisioning is "
                            "idempotent, so a retry never bills twice"}
        # ensure the endpoint is in the index so the watch has something to read
        fp = trustindex.fingerprint(norm)
        if fp not in store.trust_index:
            store.trust_index[fp] = trustindex.new_entry(norm, "watch_request")
            if store.backend is not None:
                store._persist_kv("trust_index", store.trust_index)
        rec = {
            "id": wid, "owner_key": owner_key, "endpoint": norm,
            "endpoint_id": fp, "interval_s": interval,
            "created_at": _iso(), "last_cycle_at": None,
            "cycles_billed": 0, "credits_spent": 0,
            "active": True, "last_status": None,
            "changes": [],
        }
        store.watches[wid] = rec
        if store.backend is not None:
            store._persist_kv("watches", store.watches)
        store._save()
    return {**rec, "created": True}


def watch_due(store: Any) -> list[dict[str, Any]]:
    out = []
    for rec in store.watches.values():
        if not rec.get("active"):
            continue
        last = rec.get("last_cycle_at")
        if not last:
            out.append(rec)
            continue
        try:
            when = datetime.fromisoformat(last)
        except (TypeError, ValueError):
            out.append(rec)
            continue
        if _now() - when >= timedelta(seconds=int(rec.get("interval_s", 3600))):
            out.append(rec)
    return out


def run_watch_cycle(store: Any, rec: dict[str, Any], *,
                    charge: Optional[Callable] = None,
                    runner: Optional[Callable] = None) -> dict[str, Any]:
    """One watch cycle: observe, record any CHANGE, then charge.

    Order matters. The charge happens AFTER the observation succeeds, so a
    failed cycle is never billed. If the charge itself fails (out of credits),
    the observation is kept — we already did the work and the customer should
    still see it — and the watch is suspended rather than silently continuing
    to consume our outbound budget for free."""
    view = recheck_one(store, rec["endpoint_id"], runner=runner)
    if view is None:
        return {"id": rec["id"], "cycled": False, "reason": "endpoint_gone"}
    new_status = view.get("status")
    changed = rec.get("last_status") is not None and rec["last_status"] != new_status
    charged = 0
    suspended = False
    if charge is not None:
        try:
            charged = int(charge(rec["owner_key"]) or 0)
        except Exception:  # noqa: BLE001 — insufficient credits, etc.
            suspended = True
    with store.lock, store._txn():
        live = store.watches.get(rec["id"]) or rec
        if changed:
            live.setdefault("changes", []).append({
                "at": _iso(), "from": rec.get("last_status"), "to": new_status,
                "failed": (view.get("observed") or {}).get("failed", []),
            })
            live["changes"] = live["changes"][-50:]
        live["last_status"] = new_status
        live["last_cycle_at"] = _iso()
        if charged:
            live["cycles_billed"] = int(live.get("cycles_billed", 0)) + 1
            live["credits_spent"] = int(live.get("credits_spent", 0)) + charged
        if suspended:
            live["active"] = False
            live["suspended_reason"] = "payment_failed"
            live["suspended_at"] = _iso()
        store.watches[rec["id"]] = live
        if store.backend is not None:
            store._persist_kv("watches", store.watches)
        store._save()
    return {"id": rec["id"], "cycled": True, "status": new_status,
            "changed": changed, "charged_credits": charged,
            "suspended": suspended}


def watch_feed(store: Any, wid: str) -> Optional[dict[str, Any]]:
    """Machine-readable change feed for one watch. No dashboard required."""
    rec = store.watches.get(wid)
    if not rec:
        return None
    entry = store.trust_index.get(rec.get("endpoint_id") or "")
    return {
        "id": rec["id"],
        "endpoint": rec["endpoint"],
        "active": rec.get("active", False),
        "interval_s": rec.get("interval_s"),
        "last_cycle_at": rec.get("last_cycle_at"),
        "cycles_billed": rec.get("cycles_billed", 0),
        "credits_spent": rec.get("credits_spent", 0),
        "current": trustindex.public_view(entry) if entry else None,
        "changes": list(reversed(rec.get("changes", []))),
        "suspended_reason": rec.get("suspended_reason"),
        "billing_note": ("charged per recheck ACTUALLY performed; a cycle that "
                         "could not observe the endpoint is not billed"),
    }
