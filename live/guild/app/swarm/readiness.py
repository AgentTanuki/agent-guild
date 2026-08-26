"""Signed, capability-level readiness evidence for machine callers.

Service health and an OpenAPI declaration do not prove that one operation can
complete. Terminal canaries are cached, single-flight and hard-bounded so a
public GET cannot become a work amplifier. Dependency facts are published as
a digest only; caller-specific admission remains explicitly unevaluated.
"""
from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from threading import Lock
from typing import Any

from ..crypto import canonicalize_jcs, sign_jcs
from .capabilities import CAPABILITIES, Capability, run_fixtures

SCHEMA_VERSION = "ag-capability-readiness/1"
FRESH_FOR_SECONDS = 300
REFRESH_TIMEOUT_SECONDS = 2.0

_cache_lock = Lock()
_record_cache: dict[str, dict[str, Any]] = {}
_cache_expires_monotonic = 0.0
_refresh_disabled = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _package(name: str) -> str:
    try:
        return package_version(name)
    except PackageNotFoundError:
        return "absent"


def _digest(value: Any) -> str:
    raw = canonicalize_jcs(value).encode("utf-8")
    return "sha256:" + sha256(raw).hexdigest()


def _dependency_facts(cap: Capability) -> dict[str, Any]:
    """Capability-scoped runtime fingerprint, deliberately not a full SBOM."""
    facts: dict[str, Any] = {
        "python": platform.python_version(),
        "jsonschema": _package("jsonschema"),
        "capability_id": cap.id,
        "capability_version": cap.version,
        "implementation_module": cap.run.__module__,
        "execution_class": "in_process_deterministic",
    }
    if cap.id == "text.date_normalize":
        facts["python-dateutil"] = _package("python-dateutil")
    return facts


def _record_from_result(cap: Capability, result: dict[str, Any],
                        observed_at: datetime | None = None) -> dict[str, Any]:
    has_canary = bool(result.get("terminal_canary_present", result["total"] > 0))
    if observed_at is not None:
        observed = observed_at
    else:
        raw_observed = result.get("terminal_observed_at")
        observed = datetime.fromisoformat(raw_observed) if raw_observed else _now()
    state = "ready" if result["ok"] else "failed" if has_canary else "unknown"
    terminal_observed = observed.isoformat() if has_canary else None
    fresh_until = ((observed + timedelta(seconds=FRESH_FOR_SECONDS)).isoformat()
                   if has_canary else None)
    return {
        "capability_id": cap.id,
        "capability_version": cap.version,
        "readiness_state": state,
        "callability_state": "not_evaluated",
        "canary_scope": "in_process_terminal_fixture_suite",
        "dependency_set_digest": _digest(_dependency_facts(cap)),
        "dependency_digest_scope": (
            "capability runtime fingerprint, not a transitive SBOM"
        ),
        "dependency_disclosure": "digest_only",
        "last_terminal_observed_at": terminal_observed,
        "last_terminal_outcome": (
            "all_fixtures_passed" if result["ok"]
            else "fixture_failure" if has_canary else "missing_canary"
        ),
        "last_terminal_result_digest": (
            result.get("terminal_result_digest") if has_canary else None
        ),
        "fresh_until": fresh_until,
        "fresh_for_seconds": FRESH_FOR_SECONDS if has_canary else None,
        "fixture_summary": {
            "total": result["total"],
            "passed": result["passed"],
            "failed": result["failed"],
            "fixture_gate_passed": result["ok"],
            "output_schema_gate_passed": result.get(
                "output_schema_passed", result["ok"]),
        },
        "limits": [
            "proves the capability implementation completed its bounded local canary",
            "caller and quota admission are not evaluated by this document",
            "does not prove an external dependency that the canary did not exercise",
        ],
    }


def capability_record(cap: Capability, *,
                      observed_at: datetime | None = None) -> dict[str, Any]:
    """Run one terminal canary. Public routes use the bounded cache below."""
    return _record_from_result(cap, run_fixtures(cap), observed_at)


def prime_cache(gate_results: dict[str, dict[str, Any]]) -> None:
    """Prime from the publish gate already executed during controlled startup."""
    global _cache_expires_monotonic, _refresh_disabled
    with _cache_lock:
        _record_cache.clear()
        for cap_id, result in gate_results.items():
            cap = CAPABILITIES.get(cap_id)
            if cap is not None:
                _record_cache[cap_id] = _record_from_result(cap, result)
        _cache_expires_monotonic = time.monotonic() + FRESH_FOR_SECONDS
        _refresh_disabled = False


def clear_cache_for_tests() -> None:
    global _cache_expires_monotonic, _refresh_disabled
    with _cache_lock:
        _record_cache.clear()
        _cache_expires_monotonic = 0.0
        _refresh_disabled = False


def _bounded_refresh_locked() -> None:
    """Refresh single-flight without letting a hung canary hold a public GET.

    Canaries execute in a child process. On deadline the process is killed and
    reaped, so a looping implementation cannot continue consuming resources.
    A failed refresh is not retried in this server process; stale evidence then
    fails closed until the next controlled restart primes the cache.
    """
    global _cache_expires_monotonic, _refresh_disabled
    if _refresh_disabled:
        return
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed interpreter/module
            [sys.executable, "-m", "app.swarm.readiness", "--canary-worker"],
            cwd=str(Path(__file__).resolve().parents[2]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError:
        _refresh_disabled = True
        return
    try:
        stdout, _stderr = process.communicate(timeout=REFRESH_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        _refresh_disabled = True
        return
    if process.returncode != 0:
        _refresh_disabled = True
        return
    try:
        records = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        _refresh_disabled = True
        return
    if (not isinstance(records, dict)
            or set(records) != set(CAPABILITIES)
            or any(not _valid_worker_record(cap_id, record)
                   for cap_id, record in records.items())):
        _refresh_disabled = True
        return
    _record_cache.clear()
    _record_cache.update(records)
    _cache_expires_monotonic = time.monotonic() + FRESH_FOR_SECONDS


def _aware_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _sha256_digest(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    raw = value.removeprefix("sha256:")
    return len(raw) == 64 and all(char in "0123456789abcdef" for char in raw)


def _valid_worker_record(cap_id: str, record: Any) -> bool:
    """Validate the child protocol before any record can be cached or signed."""
    if not isinstance(record, dict):
        return False
    required = {
        "capability_id", "capability_version", "readiness_state",
        "callability_state", "canary_scope", "dependency_set_digest",
        "last_terminal_observed_at", "last_terminal_outcome",
        "last_terminal_result_digest", "fresh_until", "fixture_summary",
    }
    if not required <= set(record):
        return False
    cap = CAPABILITIES.get(cap_id)
    if (cap is None or record["capability_id"] != cap_id
            or record["capability_version"] != cap.version
            or record["callability_state"] != "not_evaluated"
            or record["canary_scope"] != "in_process_terminal_fixture_suite"
            or record["dependency_set_digest"] != _digest(
                _dependency_facts(cap))):
        return False
    state = record["readiness_state"]
    summary = record["fixture_summary"]
    if state == "unknown":
        return (record["last_terminal_observed_at"] is None
                and record["last_terminal_outcome"] == "missing_canary"
                and record["last_terminal_result_digest"] is None
                and record["fresh_until"] is None
                and isinstance(summary, dict)
                and summary.get("total") == 0
                and summary.get("fixture_gate_passed") is False)
    if state not in {"ready", "failed"} or not isinstance(summary, dict):
        return False
    observed = _aware_timestamp(record["last_terminal_observed_at"])
    fresh_until = _aware_timestamp(record["fresh_until"])
    if (observed is None or fresh_until is None
            or fresh_until != observed + timedelta(seconds=FRESH_FOR_SECONDS)
            or not _sha256_digest(record["last_terminal_result_digest"])
            or not isinstance(summary.get("total"), int)
            or summary["total"] <= 0
            or not isinstance(summary.get("passed"), int)
            or not isinstance(summary.get("failed"), int)
            or summary["passed"] + summary["failed"] != summary["total"]
            or not isinstance(summary.get("output_schema_gate_passed"), bool)):
        return False
    if state == "ready":
        return (record["last_terminal_outcome"] == "all_fixtures_passed"
                and summary["passed"] == summary["total"]
                and summary["failed"] == 0
                and summary.get("fixture_gate_passed") is True
                and summary["output_schema_gate_passed"] is True)
    return (record["last_terminal_outcome"] == "fixture_failure"
            and summary["failed"] > 0
            and summary.get("fixture_gate_passed") is False)


def _missing_record(cap: Capability, reason: str) -> dict[str, Any]:
    return {
        "capability_id": cap.id,
        "capability_version": cap.version,
        "readiness_state": "unknown",
        "callability_state": "not_evaluated",
        "canary_scope": "in_process_terminal_fixture_suite",
        "dependency_set_digest": _digest(_dependency_facts(cap)),
        "dependency_digest_scope": (
            "capability runtime fingerprint, not a transitive SBOM"
        ),
        "dependency_disclosure": "digest_only",
        "last_terminal_observed_at": None,
        "last_terminal_outcome": reason,
        "last_terminal_result_digest": None,
        "fresh_until": None,
        "fresh_for_seconds": None,
        "fixture_summary": None,
        "limits": [
            "no current terminal canary is available",
            "caller and quota admission are not evaluated by this document",
        ],
    }


def _present(record: dict[str, Any], *, global_gate_open: bool,
             now: datetime) -> dict[str, Any]:
    item = deepcopy(record)
    observed_state = item["readiness_state"]
    fresh_until = item.get("fresh_until")
    if fresh_until and now > datetime.fromisoformat(fresh_until):
        item["readiness_state"] = "unknown_stale"
    item["last_observed_readiness_state"] = observed_state
    item["global_gate_state"] = "open" if global_gate_open else "blocked"
    item["callability_state"] = (
        "not_evaluated" if global_gate_open else "blocked_global_gate"
    )
    return item


def _cached_records(selected: list[Capability]) -> list[dict[str, Any]]:
    with _cache_lock:
        if time.monotonic() >= _cache_expires_monotonic:
            _bounded_refresh_locked()
        return [deepcopy(_record_cache.get(cap.id) or _missing_record(
            cap, "canary_unavailable_or_refresh_timeout")) for cap in selected]


def readiness_document(guild_identity: dict[str, Any], *,
                       capability_id: str | None = None,
                       global_gate_open: bool = True,
                       generated_at: datetime | None = None) -> dict[str, Any]:
    """Build and sign either the complete or single-capability observation."""
    selected = (
        [CAPABILITIES[capability_id]] if capability_id is not None
        else [CAPABILITIES[key] for key in sorted(CAPABILITIES)]
    )
    cached = _cached_records(selected)
    generated = generated_at or _now()
    terminal_times = [
        parsed for parsed in (
            _aware_timestamp(record.get("last_terminal_observed_at"))
            for record in cached
        ) if parsed is not None
    ]
    if terminal_times and generated < max(terminal_times):
        generated = max(terminal_times)
    records = [_present(record, global_gate_open=global_gate_open, now=generated)
               for record in cached]
    body = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated.isoformat(),
        "transport_state": {
            "state": "reachable",
            "evidence_scope": "this successful HTTP response only",
        },
        "model_state": {
            "state": "not_applicable",
            "reason": "published Guild utility capabilities are deterministic and model-free",
        },
        "admission_state": {
            "global_gate_state": "open" if global_gate_open else "blocked",
            "caller_admission": "not_evaluated",
            "unevaluated_factors": [
                "per-caller guest or member daily quota",
                "global per-minute quota",
                "request payload validity",
            ],
        },
        "capability_count": len(records),
        "capabilities": records,
        "consumer_rule": (
            "Treat a record as current only before fresh_until. Keep transport, "
            "model, global gate, caller admission and capability readiness as "
            "separate facts; not_evaluated never means callable."
        ),
        "refresh_policy": {
            "cache_seconds": FRESH_FOR_SECONDS,
            "single_flight": True,
            "hard_refresh_timeout_seconds": REFRESH_TIMEOUT_SECONDS,
            "timeout_effect": (
                "retain prior evidence as unknown_stale, or report unknown when "
                "none exists; terminate the canary worker and never claim ready"
            ),
        },
        "verify_against": "/.well-known/agent-guild-did.json",
    }
    signature = sign_jcs(body, guild_identity["private_key"])
    return {
        "readiness": body,
        "signature": {
            "alg": "Ed25519",
            "over": "JCS(readiness)",
            "signature": signature,
            "public_key": guild_identity["public_key"],
            "signer_did": guild_identity["did"],
        },
    }


def _canary_worker() -> int:
    """One-shot subprocess protocol for killable terminal-canary refreshes."""
    records = {
        cap_id: capability_record(CAPABILITIES[cap_id])
        for cap_id in sorted(CAPABILITIES)
    }
    sys.stdout.write(json.dumps(records, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__" and sys.argv[1:] == ["--canary-worker"]:
    raise SystemExit(_canary_worker())
