"""Best-effort payment observations, never settlement authority.

Only fixed stage/reason codes and a fresh per-invocation random identifier are
written. No credentials, wallet addresses, request hashes, URLs or client labels
enter this stream. Retries get new identifiers; these are not buyers or payments.
Result preparation is observable; network delivery and buyer consumption are not.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import uuid
import threading

from .billing import PRICING
from .pricing import DEFAULTS

OPERATIONS = frozenset(PRICING) | frozenset(DEFAULTS) | {"protected_payment_decision"}

VERSION = "payment-stages-v1"
STAGES = (
    "payment_submission_present", "credential_present", "credential_parsed",
    "credential_ignored",
    "binding_valid", "recovery_started", "facilitator_verify_started",
    "facilitator_verified", "facilitator_settle_started",
    "facilitator_settlement_accepted", "chain_confirmation_started",
    "chain_confirmed", "authorization_accepted", "sandbox_authorized",
    "free_authorized", "result_prepared", "cached_result_prepared",
    "rejected", "unresolved", "result_finalize_failed", "request_failed",
)
REASONS = frozenset((
    "none", "other", "malformed_credential", "conflicting_credentials",
    "v1_not_accepted", "x402_disabled", "x402_misconfigured",
    "mpp_invalid_challenge", "mpp_payment_expired", "mpp_verification_failed",
    "mpp_credential_rejected",
    "invalid_x402_version", "method_mismatch", "requirements_mismatch",
    "resource_mismatch", "invalid_payload", "amount_mismatch",
    "recipient_mismatch", "authorization_not_yet_valid",
    "authorization_expired", "caller_payer_mismatch", "network_mismatch",
    "invalid_network", "invalid_payment_identifier", "payment_identifier_conflict",
    "payment_in_progress", "replay_rejected", "double_settlement_rejected",
    "duplicate_transaction", "recovery_anchor_unavailable",
    "settlement_state_unknown", "settlement_unconfirmed",
    "facilitator_verify_error", "facilitator_verify_rejected",
    "facilitator_settle_error", "facilitator_settle_rejected",
    "malformed_settlement_response", "unknown_billing_key",
    "insufficient_credits", "payment_required", "payment_rejected",
    "result_finalize_error", "server_error", "unknown_task",
))
_active: ContextVar["Attempt | None"] = ContextVar("payment_diagnostic", default=None)


@dataclass
class Attempt:
    operation: str
    transport: str
    first_party: bool = False
    identifier: str = field(default_factory=lambda: uuid.uuid4().hex)
    seen: set = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    started: bool = False

    def emit(self, stage: str, reason: str = "none") -> None:
        if stage not in STAGES:
            return
        reason = reason if isinstance(reason, str) and reason in REASONS else "other"
        marker = (stage, reason)
        with self.lock:
            if stage in ("credential_present", "payment_submission_present"):
                self.started = True
            if not self.started or marker in self.seen:
                return
            self.seen.add(marker)
        try:
            from .state import store
            store.record_event(
                None, "payment_stage_observed", diagnostic_version=VERSION,
                attempt_id=self.identifier,
                operation=self.operation if self.operation in OPERATIONS else "unknown",
                transport=self.transport if self.transport in ("http", "mcp", "a2a") else "unknown",
                stage=stage, reason_code=reason,
                traffic_class="known_first_party" if self.first_party else "unclassified",
                fp=self.first_party,
            )
        except Exception:
            # Telemetry cannot interrupt a payment or leak an exception that
            # might contain a credential. Financial durability lives elsewhere.
            pass


def current() -> Attempt | None:
    return _active.get()


def emit(stage: str, reason: str = "none") -> None:
    attempt = current()
    if attempt is not None:
        attempt.emit(stage, reason)


def reject(reason: str = "payment_rejected") -> None:
    emit("rejected", reason)


@contextmanager
def observe(operation: str, transport: str, first_party: bool = False):
    prior = current()
    if prior is not None:
        yield prior
        return
    attempt = Attempt(operation, transport, first_party is True)
    token = _active.set(attempt)
    try:
        yield attempt
    except Exception as exc:
        # CachedPaidResult is a control-flow return, not a request failure.
        if (type(exc).__name__ != "CachedPaidResult"
                and not any(s in ("rejected", "unresolved") for s, _ in attempt.seen)):
            attempt.emit("request_failed", "server_error")
        raise
    finally:
        _active.reset(token)


def confirm(check, *args, **kwargs):
    """Observe a real confirmation call, including settlement recovery."""
    emit("chain_confirmation_started")
    result = check(*args, **kwargs)
    if result.get("confirmed") is True:
        emit("chain_confirmed")
    else:
        emit("unresolved", "settlement_unconfirmed")
    return result


def summary(store, operation=None):
    events, coverage = store.measurement_event_view(types=("payment_stage_observed",))
    rows = {}
    first_at = last_at = None
    for event in events:
        if event.get("diagnostic_version") != VERSION:
            continue
        op = event.get("operation")
        if operation is not None and op != operation:
            continue
        stage, reason = event.get("stage"), event.get("reason_code")
        transport, traffic = event.get("transport"), event.get("traffic_class")
        if (not all(isinstance(value, str) for value in
                    (stage, reason, op, transport, traffic))
                or stage not in STAGES or reason not in REASONS
                or op not in OPERATIONS | {"unknown"}
                or transport not in ("http", "mcp", "a2a", "unknown")
                or traffic not in ("known_first_party", "unclassified")):
            continue
        at = str(event.get("at") or "")
        first_at = min(first_at, at) if first_at else at
        last_at = max(last_at, at) if last_at else at
        key = (op, transport, traffic, stage, reason)
        rows[key] = rows.get(key, 0) + 1
    return {
        "version": VERSION,
        "operation_scope": operation or "all_operations",
        "measure": "Observed server stages for invocations presenting a payment credential or A2A payment submission, including retries and internal traffic; not unique buyers, payments, revenue or a conversion rate.",
        "rows": [dict(operation=k[0], transport=k[1], traffic_class=k[2],
                      stage=k[3], reason_code=k[4], observations=v)
                 for k, v in sorted(rows.items())],
        "first_observed_at": first_at, "last_observed_at": last_at,
        "measurement_coverage": coverage,
        "limitations": (
            "Prospective, best-effort diagnostics beginning at priced gateways; "
            "earlier routing, schema and caller-proof failures are not covered. "
            "No historical backfill. "
            "A crash or telemetry failure can leave stages missing. Unclassified "
            "traffic is not proved external. Ordinary unpaid and sandbox requests "
            "without a payment credential do not create diagnostic rows; existing "
            "challenge reports cover that traffic. Zero observed credentials "
            "does not mean no wallet, no budget or price rejection. "
            "result_prepared means bytes and receipt were prepared server-side, "
            "not delivered or consumed. Recovery and cached replay need not "
            "repeat facilitator stages. Use /billing/revenue for settlement totals."),
    }
