"""AGSM-1 buyer-owned cumulative spend authority for autonomous wallets.

AGSM-1 is deliberately not a counterparty trust oracle. It answers one narrow,
repeatable pre-signing question: does this exact payment still fit the payer's
durable authority? Creation and authorization are free during a 21-day
falsification window. Production SQLite uses one row per mandate and serializes
each read-modify-write with BEGIN IMMEDIATE; the JSON backend is supported only
for a single process.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from . import crypto, vc

CONTRACT = "AGSM-1/1.0"
DEFAULT_EXPIRES_S = 7 * 86400
MAX_EXPIRES_S = 30 * 86400
MIN_WINDOW_S = 60
MAX_WINDOW_S = 30 * 86400
MAX_AUTHORIZATIONS = 500
MAX_LIFETIME_AUTHORIZATIONS = 2_000
MAX_OWNER_MANDATES = 10
MAX_JSON_MANDATES = 10_000
MAX_SQLITE_MANDATES = 500
MAX_METRIC_ROWS = 50_000
AUTHORIZATION_TTL_S = 300
EXPERIMENT_WINDOW_DAYS = 21
EXPERIMENT_KEY = "agsm1-free-authority-2026-08"
_ATOMIC = re.compile(r"^[0-9]+$")
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


class SpendMandateRefused(ValueError):
    code = "spend_mandate_refused"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: str, label: str) -> datetime:
    try:
        out = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise SpendMandateRefused(f"{label} is malformed") from exc
    if out.tzinfo is None:
        raise SpendMandateRefused(f"{label} must be timezone-aware")
    return out.astimezone(timezone.utc)


def _positive_atomic(value: Any, label: str) -> str:
    out = str(value or "").strip()
    if (not _ATOMIC.fullmatch(out) or len(out) > 78
            or int(out) <= 0 or int(out) >= 2 ** 256):
        raise SpendMandateRefused(
            f"{label} must be a positive atomic-unit integer string")
    return out


def _bounded_int(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise SpendMandateRefused(f"{label} must be an integer")
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise SpendMandateRefused(f"{label} must be an integer") from exc
    if out < low or out > high:
        raise SpendMandateRefused(f"{label} must be in [{low}, {high}]")
    return out


def _address(value: Any, label: str) -> str:
    out = str(value or "").strip().lower()
    if not _EVM_ADDRESS.fullmatch(out):
        raise SpendMandateRefused(f"{label} must be an exact EVM address")
    return out


def _digest(value: Any) -> str:
    return hashlib.sha256(
        crypto.canonicalize_jcs(value).encode("utf-8")).hexdigest()


def _identifier_digest(value: str) -> str:
    """Hash an opaque identifier as UTF-8, matching SDK implementations."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def metric_actor(store: Any, caller_did: str) -> str:
    """Unlinkable outside this service: never publish a raw/hashable EOA."""
    secret = str(getattr(store, "spend_mandate_metric_secret", "") or "")
    if not secret:
        raise SpendMandateRefused("spend mandate metric identity unavailable")
    return hmac.new(
        secret.encode("utf-8"),
        ("agent-guild/agsm1-metric/" + caller_did).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _first_party_eoa(caller_did: str) -> bool:
    address = owner_address(caller_did)
    configured = {
        item.strip().lower() for item in (
            os.environ.get("GUILD_X402_FIRST_PARTY_PAYERS") or "").split(",")
        if item.strip()
    }
    return address in configured


def first_party_metric_actors(store: Any) -> set[str]:
    """Read-time demotion: newly disclosed canary wallets heal history."""
    if not getattr(store, "spend_mandate_metric_secret", ""):
        return set()
    out: set[str] = set()
    for item in (os.environ.get(
            "GUILD_X402_FIRST_PARTY_PAYERS") or "").split(","):
        address = item.strip().lower()
        if _EVM_ADDRESS.fullmatch(address):
            did = f"did:pkh:eip155:8453:{address}"
            out.add(metric_actor(store, did))
    return out


def ensure_experiment(store: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Persist the exact free-treatment window once; redeploys cannot reset it."""
    clock = (now or _now()).astimezone(timezone.utc)
    with store.lock, store._txn():
        secret_created = False
        if not getattr(store, "spend_mandate_metric_secret", ""):
            store.spend_mandate_metric_secret = secrets.token_hex(32)
            secret_created = True
            if store.backend is not None:
                store._persist_kv(
                    "spend_mandate_metric_secret",
                    store.spend_mandate_metric_secret)
        existing = getattr(store, "spend_mandate_experiment", None)
        if existing:
            if secret_created:
                store._save()
            return existing
        rec = {
            "key": EXPERIMENT_KEY,
            "contract": CONTRACT,
            "hypothesis": (
                "one genuine external EOA creates a mandate and receives at "
                "least two non-replay authorizations on it"),
            "started_at": clock.isoformat(),
            "ends_at": (clock + timedelta(days=EXPERIMENT_WINDOW_DAYS)).isoformat(),
            "window_days": EXPERIMENT_WINDOW_DAYS,
            "treatment": "free_creation_and_budget_authorization_only",
            "status": "running",
        }
        store.spend_mandate_experiment = rec
        if store.backend is not None:
            store._persist_kv("spend_mandate_experiment", rec)
        store._save()
        return rec


def _persist_metric(store: Any, metric: dict[str, Any]) -> None:
    store.spend_mandate_metrics[metric["mandate_id"]] = metric
    if store.backend is not None:
        store.backend.put_spend_mandate_metric(metric)


def owner_address(caller_did: str) -> str:
    prefix = "did:pkh:eip155:8453:"
    if not isinstance(caller_did, str) or not caller_did.startswith(prefix):
        raise SpendMandateRefused(
            "a verified Base-mainnet EOA caller proof is required")
    return _address(caller_did[len(prefix):], "caller proof address")


def normalise_create(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise SpendMandateRefused("request must be an object")
    allowed = {"network", "asset", "caps", "new_payee_cooldown_s", "expires_s"}
    unknown = sorted(set(request) - allowed)
    if unknown:
        raise SpendMandateRefused("unsupported fields: " + ", ".join(unknown))
    network = str(request.get("network") or "").strip()
    if network != "eip155:8453":
        raise SpendMandateRefused("network must be eip155:8453")
    asset = _address(request.get("asset"), "asset")
    caps = request.get("caps")
    if not isinstance(caps, dict):
        raise SpendMandateRefused("caps must be an object")
    cap_fields = {"window_s", "max_atomic", "per_counterparty_atomic",
                  "max_authorizations"}
    cap_unknown = sorted(set(caps) - cap_fields)
    cap_missing = sorted(
        {"window_s", "max_atomic", "max_authorizations"} - set(caps))
    if cap_unknown:
        raise SpendMandateRefused(
            "unsupported cap fields: " + ", ".join(cap_unknown))
    if cap_missing:
        raise SpendMandateRefused(
            "missing cap fields: " + ", ".join(cap_missing))
    maximum = _positive_atomic(caps.get("max_atomic"), "max_atomic")
    per_raw = caps.get("per_counterparty_atomic")
    per_counterparty = (_positive_atomic(per_raw, "per_counterparty_atomic")
                        if per_raw is not None else maximum)
    if int(per_counterparty) > int(maximum):
        raise SpendMandateRefused(
            "per_counterparty_atomic cannot exceed max_atomic")
    window = _bounded_int(
        caps.get("window_s"), "window_s", MIN_WINDOW_S, MAX_WINDOW_S)
    expires = _bounded_int(
        request.get("expires_s", DEFAULT_EXPIRES_S), "expires_s",
        MIN_WINDOW_S, MAX_EXPIRES_S)
    return {
        "network": network,
        "asset": asset,
        "caps": {
            "window_s": window,
            "max_atomic": maximum,
            "per_counterparty_atomic": per_counterparty,
            "max_authorizations": _bounded_int(
                caps.get("max_authorizations"), "max_authorizations", 1,
                MAX_AUTHORIZATIONS),
            "max_lifetime_authorizations": MAX_LIFETIME_AUTHORIZATIONS,
        },
        "new_payee_cooldown_s": _bounded_int(
            request.get("new_payee_cooldown_s", 0),
            "new_payee_cooldown_s", 0, window),
        "expires_s": expires,
    }


def normalise_reference(mandate_id: Any, authorization_id: Any) -> tuple[str, str]:
    mid = str(mandate_id or "").strip()
    aid = str(authorization_id or "").strip()
    if not _IDENTIFIER.fullmatch(mid) or not mid.startswith("agsm_"):
        raise SpendMandateRefused("mandate_id is malformed")
    if not _IDENTIFIER.fullmatch(aid):
        raise SpendMandateRefused(
            "authorization_id must be 8..128 safe characters")
    return mid, aid


def normalise_authorization(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise SpendMandateRefused("request must be an object")
    allowed = {"mandate_id", "authorization_id", "payment"}
    unknown = sorted(set(request) - allowed)
    missing = sorted(allowed - set(request))
    if unknown:
        raise SpendMandateRefused("unsupported fields: " + ", ".join(unknown))
    if missing:
        raise SpendMandateRefused("missing fields: " + ", ".join(missing))
    mandate_id, authorization_id = normalise_reference(
        request.get("mandate_id"), request.get("authorization_id"))
    raw = request.get("payment")
    if not isinstance(raw, dict):
        raise SpendMandateRefused("payment must be an object")
    fields = {"scheme", "network", "asset", "amount", "pay_to", "resource"}
    unknown_payment = sorted(set(raw) - fields)
    missing_payment = sorted(fields - set(raw))
    if unknown_payment:
        raise SpendMandateRefused(
            "unsupported payment fields: " + ", ".join(unknown_payment))
    if missing_payment:
        raise SpendMandateRefused(
            "missing payment fields: " + ", ".join(missing_payment))
    scheme = str(raw.get("scheme") or "").strip()
    if scheme != "exact":
        raise SpendMandateRefused("payment scheme must be exact")
    network = str(raw.get("network") or "").strip()
    if network != "eip155:8453":
        raise SpendMandateRefused("payment network must be eip155:8453")
    resource = str(raw.get("resource") or "").strip()
    parsed = urlsplit(resource)
    if (parsed.scheme not in ("http", "https") or not parsed.netloc
            or parsed.username is not None or parsed.password is not None
            or len(resource) > 2048):
        raise SpendMandateRefused(
            "payment resource must be an http(s) URL without credentials")
    return {
        "mandate_id": mandate_id,
        "authorization_id": authorization_id,
        "payment": {
            "scheme": scheme,
            "network": network,
            "asset": _address(raw.get("asset"), "payment asset"),
            "amount": _positive_atomic(raw.get("amount"), "payment amount"),
            "pay_to": _address(raw.get("pay_to"), "payment pay_to"),
            "resource": resource,
        },
    }


def _ensure_process_safe(store: Any) -> None:
    if store.backend is not None:
        return
    workers = 1
    for name in ("WEB_CONCURRENCY", "GUILD_WORKERS", "UVICORN_WORKERS"):
        try:
            workers = max(workers, int(os.environ.get(name) or 0))
        except ValueError:
            pass
    match = re.search(r"--workers[=\s]+(\d+)", " ".join(sys.argv))
    if match:
        workers = max(workers, int(match.group(1)))
    if workers > 1:
        raise SpendMandateRefused(
            "AGSM-1 requires SQLite when more than one worker process is configured")


def _prune(store: Any, clock: datetime) -> None:
    if store.backend is not None:
        store.backend.prune_spend_mandates(clock.isoformat())
        store.spend_mandates = {
            mid: rec for mid, rec in store.spend_mandates.items()
            if _aware(rec["expires_at"], "expires_at") > clock
        }
        return
    expired = [mid for mid, rec in store.spend_mandates.items()
               if _aware(rec["expires_at"], "expires_at") <= clock]
    for mid in expired:
        del store.spend_mandates[mid]


def _load(store: Any, mandate_id: str) -> dict[str, Any] | None:
    if store.backend is not None:
        rec = store.backend.fetch_spend_mandate(mandate_id)
        if rec is not None:
            store.spend_mandates[mandate_id] = rec
        return rec
    return store.spend_mandates.get(mandate_id)


def _persist(store: Any, mandate: dict[str, Any]) -> None:
    store.spend_mandates[mandate["mandate_id"]] = mandate
    if store.backend is not None:
        store.backend.put_spend_mandate(mandate)
    store._save()


def _mandate_credential(store: Any, mandate: dict[str, Any],
                        issued: datetime) -> dict[str, Any]:
    gid = store.guild_identity()
    if not gid.get("did") or not gid.get("private_key"):
        raise SpendMandateRefused("Guild signing identity unavailable")
    unsigned = {
        "@context": vc.VC_CONTEXT_V2,
        "id": "urn:agent-guild:spend-mandate:" + mandate["mandate_id"],
        "type": ["VerifiableCredential", "AgentGuildSpendMandate"],
        "issuer": gid["did"],
        "validFrom": mandate["created_at"],
        "validUntil": mandate["expires_at"],
        "credentialSubject": {
            "id": mandate["owner_did"],
            "contract": CONTRACT,
            "mandate_id": mandate["mandate_id"],
            "mandate_digest": mandate["mandate_digest"],
            "network": mandate["network"],
            "asset": mandate["asset"],
            "caps": mandate["caps"],
            "new_payee_cooldown_s": mandate["new_payee_cooldown_s"],
            "limits": (
                "This proves declared authorization limits, not custody, "
                "settlement, provider trust, delivery, or insurance."),
        },
    }
    return vc._add_data_integrity_proof(
        unsigned, gid["did"], gid["private_key"], issued.isoformat())


def create(store: Any, request: Any, *, caller_did: str,
           now: datetime | None = None, first_party: bool = False) -> dict[str, Any]:
    normalized = normalise_create(request)
    owner_address(caller_did)
    _ensure_process_safe(store)
    issued = (now or _now()).astimezone(timezone.utc)
    experiment = ensure_experiment(store, now=issued)
    if issued >= _aware(experiment["ends_at"], "experiment ends_at"):
        raise SpendMandateRefused("AGSM-1 free falsification window is closed")
    mandate_id = "agsm_" + secrets.token_urlsafe(18).replace("-", "_")
    immutable = {
        "contract": CONTRACT,
        "mandate_id": mandate_id,
        "owner_did": caller_did,
        **normalized,
        "created_at": issued.isoformat(),
        "expires_at": (issued + timedelta(
            seconds=normalized["expires_s"])).isoformat(),
    }
    mandate = {
        **immutable,
        "mandate_digest": _digest(immutable),
        "spent_atomic": "0",
        "authorization_count": 0,
        "window_started_at": issued.isoformat(),
        "per_counterparty": {},
        "authorizations": {},
        "known_payees": [],
        "last_new_payee_at": None,
        "status": "active",
    }
    mandate["credential"] = _mandate_credential(store, mandate, issued)
    with store.lock, store._txn():
        _prune(store, issued)
        if store.backend is not None:
            owner_count = store.backend.count_spend_mandates(
                caller_did, issued.isoformat())
            if store.backend.count_spend_mandates_total() >= MAX_SQLITE_MANDATES:
                raise SpendMandateRefused("spend mandate store is at capacity")
            if store.backend.count_spend_mandate_metrics() >= MAX_METRIC_ROWS:
                raise SpendMandateRefused("spend mandate experiment is at capacity")
        else:
            owner_count = sum(
                1 for rec in store.spend_mandates.values()
                if rec.get("owner_did") == caller_did
                and rec.get("status") == "active")
            if len(store.spend_mandates) >= MAX_JSON_MANDATES:
                raise SpendMandateRefused("spend mandate store is at capacity")
            if len(store.spend_mandate_metrics) >= MAX_METRIC_ROWS:
                raise SpendMandateRefused("spend mandate experiment is at capacity")
        if owner_count >= MAX_OWNER_MANDATES:
            raise SpendMandateRefused("owner has too many active spend mandates")
        _persist_metric(store, {
            "mandate_id": mandate_id,
            "actor_hmac": metric_actor(store, caller_did),
            "created_at": issued.isoformat(),
            "external_eligible": bool(
                not first_party and not _first_party_eoa(caller_did)),
            "external_authorizations": 0,
            "first_authorized_at": None,
            "last_authorized_at": None,
        })
        _persist(store, mandate)
    return public_view(mandate)


def public_view(mandate: dict[str, Any]) -> dict[str, Any]:
    return ({key: mandate.get(key) for key in (
        "contract", "mandate_id", "mandate_digest", "owner_did",
        "network", "asset", "caps", "new_payee_cooldown_s",
        "created_at", "expires_at", "status", "spent_atomic",
        "authorization_count", "per_counterparty", "known_payees",
        "last_new_payee_at", "window_started_at", "credential")}
        | ({"revoked_at": mandate["revoked_at"]}
           if mandate.get("revoked_at") else {}))


def get_owned(store: Any, mandate_id: str, caller_did: str) -> dict[str, Any]:
    owner_address(caller_did)
    with store.lock:
        mandate = _load(store, mandate_id)
        if mandate is None or mandate.get("owner_did") != caller_did:
            raise SpendMandateRefused("unknown mandate")
        return public_view(mandate)


def revoke(store: Any, mandate_id: str, caller_did: str,
           now: datetime | None = None) -> dict[str, Any]:
    owner_address(caller_did)
    clock = (now or _now()).astimezone(timezone.utc)
    with store.lock, store._txn():
        mandate = _load(store, mandate_id)
        if mandate is None or mandate.get("owner_did") != caller_did:
            raise SpendMandateRefused("unknown mandate")
        if mandate.get("status") == "active":
            mandate["status"] = "revoked"
            mandate["revoked_at"] = clock.isoformat()
            _persist(store, mandate)
        return public_view(mandate)


def _evaluate(mandate: dict[str, Any], payment: dict[str, Any],
              authorization_id: str, now: datetime) -> dict[str, Any]:
    failures: list[str] = []
    amount = int(payment["amount"])
    payee = payment["pay_to"]
    expires = _aware(mandate["expires_at"], "expires_at")
    window_started = _aware(mandate["window_started_at"], "window_started_at")
    window_reset = (now - window_started).total_seconds() >= int(
        mandate["caps"]["window_s"])
    authorization_hash = _identifier_digest(authorization_id)
    duplicate = authorization_hash in (mandate.get("authorizations") or {})
    if duplicate:
        failures.append("authorization_id_already_consumed")
    if mandate.get("status") != "active":
        failures.append("mandate_not_active")
    if now >= expires:
        failures.append("mandate_expired")
    if payment["network"] != mandate["network"]:
        failures.append("mandate_network_mismatch")
    if payment["asset"] != mandate["asset"]:
        failures.append("mandate_asset_mismatch")
    spent = 0 if window_reset else int(mandate.get("spent_atomic", "0"))
    count = 0 if window_reset else int(mandate.get("authorization_count", 0))
    payee_spent = 0 if window_reset else int(
        (mandate.get("per_counterparty") or {}).get(payee, "0"))
    caps = mandate["caps"]
    if spent + amount > int(caps["max_atomic"]):
        failures.append("mandate_total_cap_exceeded")
    if payee_spent + amount > int(caps["per_counterparty_atomic"]):
        failures.append("mandate_counterparty_cap_exceeded")
    if count + 1 > int(caps["max_authorizations"]):
        failures.append("mandate_authorization_count_exceeded")
    if len(mandate.get("authorizations") or {}) >= int(
            caps["max_lifetime_authorizations"]):
        failures.append("mandate_lifetime_authorization_limit_reached")
    known = set() if window_reset else set(mandate.get("known_payees") or [])
    last_new = None if window_reset else mandate.get("last_new_payee_at")
    cooldown = int(mandate.get("new_payee_cooldown_s") or 0)
    if payee not in known and known and last_new and cooldown:
        if (now - _aware(last_new, "last_new_payee_at")).total_seconds() < cooldown:
            failures.append("mandate_new_payee_cooldown_active")
    return {
        "failures": sorted(set(failures)),
        "duplicate": duplicate,
        "window_reset": window_reset,
        "window_started_at": now.isoformat() if window_reset
                             else window_started.isoformat(),
        "payment_sha256": _digest(payment),
        "spent_before_atomic": str(spent),
        "spent_after_atomic": str(spent + amount),
        "authorization_count_before": count,
        "authorization_count_after": count + 1,
        "payee_spent_before_atomic": str(payee_spent),
        "payee_spent_after_atomic": str(payee_spent + amount),
    }


def _authorization_credential(store: Any, mandate: dict[str, Any],
                              normalized: dict[str, Any], state: dict[str, Any],
                              issued: datetime) -> dict[str, Any]:
    gid = store.guild_identity()
    if not gid.get("did") or not gid.get("private_key"):
        raise SpendMandateRefused("Guild signing identity unavailable")
    authorization_hash = _identifier_digest(normalized["authorization_id"])
    authorized = not state["failures"]
    expires = min(
        _aware(mandate["expires_at"], "expires_at"),
        issued + timedelta(seconds=AUTHORIZATION_TTL_S))
    unsigned = {
        "@context": vc.VC_CONTEXT_V2,
        "id": "urn:agent-guild:spend-authorization:" + _digest({
            "mandate_id": mandate["mandate_id"],
            "authorization_id_sha256": authorization_hash,
            "issued": issued.isoformat(),
        }),
        "type": ["VerifiableCredential", "AgentGuildSpendAuthorization"],
        "issuer": gid["did"],
        "validFrom": issued.isoformat(),
        "validUntil": expires.isoformat(),
        "credentialSubject": {
            "id": mandate["owner_did"],
            "contract": CONTRACT,
            "mandate_id": mandate["mandate_id"],
            "mandate_digest": mandate["mandate_digest"],
            "authorization_id_sha256": authorization_hash,
            "payment": normalized["payment"],
            "payment_sha256": state["payment_sha256"],
            "authorized": authorized,
            "decision": "allow" if authorized else "block",
            "failures": state["failures"],
            "idempotent_replay": state["duplicate"],
            "spend_state": {k: v for k, v in state.items()
                            if k not in {"failures", "payment_sha256", "duplicate"}},
            "limits": (
                "Authorization is payer budget approval only. It does not "
                "attest counterparty trust, settlement, or delivery."),
        },
    }
    return vc._add_data_integrity_proof(
        unsigned, gid["did"], gid["private_key"], issued.isoformat())


def authorize_and_issue(store: Any, request: Any, *, caller_did: str,
                        now: datetime | None = None,
                        first_party: bool = False) -> dict[str, Any]:
    """Sign the exact final state and atomically consume one authorization."""
    normalized = normalise_authorization(request)
    owner_address(caller_did)
    _ensure_process_safe(store)
    clock = (now or _now()).astimezone(timezone.utc)
    with store.lock, store._txn():
        _prune(store, clock)
        experiment = ensure_experiment(store, now=clock)
        if clock >= _aware(experiment["ends_at"], "experiment ends_at"):
            raise SpendMandateRefused("AGSM-1 free falsification window is closed")
        mandate = _load(store, normalized["mandate_id"])
        # Refuse before evaluating or signing: a mandate id is not a secret,
        # and a non-owner must not obtain its owner or live spend totals.
        if mandate is None or mandate.get("owner_did") != caller_did:
            raise SpendMandateRefused("unknown mandate")
        state = _evaluate(
            mandate, normalized["payment"], normalized["authorization_id"], clock)
        decision = _authorization_credential(
            store, mandate, normalized, state, clock)
        if not state["failures"]:
            payee = normalized["payment"]["pay_to"]
            if state["window_reset"]:
                mandate["window_started_at"] = state["window_started_at"]
                mandate["spent_atomic"] = "0"
                mandate["authorization_count"] = 0
                mandate["per_counterparty"] = {}
                mandate["known_payees"] = []
                mandate["last_new_payee_at"] = None
            mandate["spent_atomic"] = state["spent_after_atomic"]
            mandate["authorization_count"] = state["authorization_count_after"]
            mandate.setdefault("per_counterparty", {})[payee] = \
                state["payee_spent_after_atomic"]
            if payee not in mandate.setdefault("known_payees", []):
                mandate["known_payees"].append(payee)
                mandate["known_payees"].sort()
                mandate["last_new_payee_at"] = clock.isoformat()
            mandate.setdefault("authorizations", {})[
                _identifier_digest(normalized["authorization_id"])] = \
                clock.isoformat()
            metric = (store.backend.fetch_spend_mandate_metric(
                mandate["mandate_id"]) if store.backend is not None
                else store.spend_mandate_metrics.get(mandate["mandate_id"]))
            if (metric and metric.get("external_eligible") is True
                    and not first_party and not _first_party_eoa(caller_did)
                    and clock < _aware(experiment["ends_at"], "experiment ends_at")):
                metric["external_authorizations"] = int(
                    metric.get("external_authorizations") or 0) + 1
                metric["first_authorized_at"] = (
                    metric.get("first_authorized_at") or clock.isoformat())
                metric["last_authorized_at"] = clock.isoformat()
                _persist_metric(store, metric)
            _persist(store, mandate)
        return decision
