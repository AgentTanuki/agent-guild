"""AGIR-1 confidential incident drop box.

The public side is intentionally one-way and non-oracular. All submissions get
fresh, same-shape signed receipts. Deduplication and raw content exist only in
the fail-closed operator view; no public instrumentation event is emitted here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import uuid
from typing import Any, Optional

from . import crypto
from . import vc


CONTRACT = "AGIR-1/1.0"
PUBLIC_RESPONSE_BYTES = 1200
CATEGORIES = frozenset({
    "unauthorized_coordination", "authority_confusion", "scope_drift",
    "credential_exposure", "unsafe_action", "infrastructure_escape", "other",
})
SEVERITIES = frozenset({"unknown", "low", "medium", "high", "critical"})


def _now() -> str:
    # Fixed-width timestamps keep novel and duplicate receipt byte lengths
    # identical. UTC offset is explicit for cross-language parsing.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate(
    *, category: str, severity: str, details: Optional[str],
    content_sha256: Optional[str], task_ref: Optional[str],
    mandate_ref: Optional[str], nonce: Optional[str],
) -> str:
    if category not in CATEGORIES:
        raise ValueError("unsupported incident category")
    if severity not in SEVERITIES:
        raise ValueError("unsupported incident severity")
    if details is None and content_sha256 is None:
        raise ValueError("provide details or content_sha256")
    if details is not None:
        detail_bytes = len(details.encode("utf-8"))
        if detail_bytes == 0:
            raise ValueError("details must not be empty")
        if detail_bytes > 8192:
            raise ValueError("details exceeds 8192 UTF-8 bytes")
    supplied_hash = (content_sha256 or "").strip()
    if supplied_hash and (len(supplied_hash) != 64
                          or any(c not in "0123456789abcdef" for c in supplied_hash)):
        raise ValueError("content_sha256 must be 64 lowercase hexadecimal characters")
    details_hash = _sha256_text(details) if details is not None else supplied_hash
    if supplied_hash and details is not None and supplied_hash != details_hash:
        raise ValueError("content_sha256 does not match details")
    for name, value in (("task_ref", task_ref), ("mandate_ref", mandate_ref)):
        if value is not None and not (1 <= len(value) <= 128):
            raise ValueError(f"{name} must contain 1..128 characters")
    if nonce is not None and not (8 <= len(nonce) <= 128):
        raise ValueError("nonce must contain 8..128 characters")
    return details_hash


def submit(
    store,
    *,
    category: str,
    severity: str = "unknown",
    details: Optional[str] = None,
    content_sha256: Optional[str] = None,
    task_ref: Optional[str] = None,
    mandate_ref: Optional[str] = None,
    nonce: Optional[str] = None,
    reporter_agent: Optional[dict[str, Any]] = None,
    transport: str = "http",
) -> dict[str, Any]:
    """Persist one private report and return only its fresh signed receipt."""
    details_hash = _validate(
        category=category, severity=severity, details=details,
        content_sha256=content_sha256, task_ref=task_ref,
        mandate_ref=mandate_ref, nonce=nonce)
    core = {
        "contract": CONTRACT,
        "category": category,
        "severity": severity,
        "content_sha256": details_hash,
        "task_ref": task_ref,
        "mandate_ref": mandate_ref,
    }
    report_sha256 = hashlib.sha256(
        crypto.canonicalize_jcs(core).encode("utf-8")).hexdigest()
    report_id = "inc_" + uuid.uuid4().hex
    received_at = _now()
    identity = store.guild_identity()
    receipt = vc.issue_incident_receipt(
        cred_id=f"urn:uuid:{uuid.uuid4()}",
        issuer_did=identity["did"],
        issuer_private_hex=identity["private_key"],
        report_subject_id=f"urn:agent-guild:incident:{report_id}",
        report_sha256=report_sha256,
        received_at=received_at,
    )

    # Duplicate grouping is private and is deliberately computed only inside
    # the durable write. It never influences the receipt, status code or body.
    with store.lock, store._txn():
        duplicate_of = store.incident_dedupe.get(report_sha256)
        if duplicate_of is None:
            store.incident_dedupe[report_sha256] = report_id
        store.incident_reports[report_id] = {
            "id": report_id,
            "contract": CONTRACT,
            "received_at": received_at,
            "category": category,
            "severity": severity,
            "details": details,
            "content_sha256": details_hash,
            "report_sha256": report_sha256,
            "task_ref": task_ref,
            "mandate_ref": mandate_ref,
            "nonce": nonce,
            "reporter_agent_id": ((reporter_agent or {}).get("id")),
            "reporter_did": ((reporter_agent or {}).get("did")),
            "transport": transport,
            "duplicate_of": duplicate_of,
            "triage": "unreviewed",
            "receipt": receipt,
        }
        if store.backend is not None:
            store._persist_kv("incident_reports", store.incident_reports)
            store._persist_kv("incident_dedupe", store.incident_dedupe)
        store._save()
    # Base58btc signatures vary by one byte when leading zeroes occur. Without
    # inert padding, even otherwise identical receipt schemas have variable
    # Content-Length. Pad the complete accepted response to one fixed size so
    # dedupe state cannot become a length oracle on any transport.
    public = {"receipt": receipt, "padding": ""}
    encoded = json.dumps(
        public, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > PUBLIC_RESPONSE_BYTES:
        raise RuntimeError("AGIR-1 public receipt exceeded fixed response budget")
    public["padding"] = "0" * (PUBLIC_RESPONSE_BYTES - len(encoded))
    return public


def operator_list(store, *, limit: int = 100) -> dict[str, Any]:
    rows = sorted(
        store.incident_reports.values(),
        key=lambda row: row.get("received_at") or "", reverse=True)[:limit]
    return {"contract": CONTRACT, "count": len(store.incident_reports),
            "reports": rows}


def operator_get(store, report_id: str) -> Optional[dict[str, Any]]:
    return store.incident_reports.get(report_id)
