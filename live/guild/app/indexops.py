"""Index operations: ingest, recheck, watch — the autonomous half of the index.

Kept out of ``store.py`` deliberately. These are the only functions in the
service that reach out to third-party infrastructure on a schedule, so they are
in one file where their bounds can be read in a single sitting:

  * ingest is default-OFF for remote sources and capped per run;
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
    added = updated = skipped = 0
    with store.lock, store._txn():
        for rec in records:
            url = (rec.get("endpoint") or "").strip()
            norm = trustindex.normalise_url(url)
            if not norm:
                skipped += 1
                continue
            fp = trustindex.fingerprint(norm)
            entry = store.trust_index.get(fp)
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
            "skipped_unusable": skipped,
            "total_entries": len(store.trust_index),
            "note": ("added = endpoints not previously known. "
                     "provenance_updated = already known, another source saw "
                     "it — NOT a new endpoint. Inventory is a supporting "
                     "metric and is never reported as adoption.")}


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
    store.record_event(None, "index_observation", endpoint="index",
                       target=entry["endpoint"], status=entry["status"],
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


def provision_watch(store: Any, owner_key: str, endpoint: str, *,
                    interval_s: int = 3600) -> dict[str, Any]:
    """Create (or return) a continuous watch. Idempotent by (owner, endpoint).

    No human is involved and none is required. Provisioning is free — charging
    before any observation exists would be charging for a promise."""
    norm = trustindex.normalise_url(endpoint)
    if not norm:
        raise ValueError("unusable endpoint url")
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
