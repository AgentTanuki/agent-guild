"""A2A x402 payments extension v0.1 (official Google A2A-x402 spec).

Spec: https://github.com/google-a2a/a2a-x402/v0.1 — declared in
/.well-known/agent-card.json. The paid A2A trust read (`check: <capability>`)
now moves through the SAME shared paid-operation gateway as HTTP and MCP
(app/payments.py), closing the free A2A bypass where a full AGD-1 decision was
returned with no payment.

The v0.1 lifecycle over `message/send`:

  1. payment-required  — the merchant (Guild) returns a Task in state
     `input-required` whose status message metadata carries
     `x402.payment.status: "payment-required"` and
     `x402.payment.required: <x402PaymentRequiredResponse>`. The Guild also
     stores the exact quote (bound to the canonical /check PaidRequest) under
     the taskId, so the later submission can be validated against what was
     actually offered — supplying the exact-resource binding the v1 wire shape
     lacks.
  2. payment-submitted — the client re-sends `message/send` with the SAME
     taskId, `x402.payment.status: "payment-submitted"` and the signed
     `x402.payment.payload` (a PaymentPayload). The Guild binds it to the
     stored quote, verifies + settles through the shared gateway, and…
  3. payment-completed — returns the Task carrying the requested result as an
     artifact plus `x402.payment.status: "payment-completed"` and the full
     `x402.payment.receipts` history (each an x402SettleResponse, incl. the
     signed offer-receipt + Guild evidence attachment).

An unpaid A2A caller of a paid trust read therefore receives a
payment-required Task, never the complete paid trust payload.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

from x402.schemas import PaymentPayload

from . import demand as demand_mod
from . import payments
from . import x402
from .payments import PaidRequest
from .state import store

EXTENSION_URI = "https://github.com/google-a2a/a2a-x402/v0.1"

STATUS_KEY = "x402.payment.status"
REQUIRED_KEY = "x402.payment.required"
PAYLOAD_KEY = "x402.payment.payload"
RECEIPTS_KEY = "x402.payment.receipts"
ERROR_KEY = "x402.payment.error"


def extension_activated(headers: Any) -> bool:
    """Client requested the extension via the X-A2A-Extensions header."""
    return EXTENSION_URI in (headers.get("x-a2a-extensions", "") or "")


def _v1_network() -> str:
    """The legacy (v0.1) network name for the configured CAIP-2 network."""
    return x402.CAIP2_TO_V1_NETWORK.get(x402.network(), x402.network())


def payment_required_response(preq: PaidRequest, credits_cost: int
                              ) -> dict[str, Any]:
    """The x402PaymentRequiredResponse (v0.1 shape) for one exact request."""
    offered = x402.requirements(credits_cost)
    return {
        "x402Version": 1,
        "accepts": [{
            "scheme": "exact",
            "network": _v1_network(),
            "resource": preq.resource_url,
            "description": f"Agent Guild paid trust read: {preq.operation}",
            "mimeType": "application/json",
            "asset": offered.asset,
            "payTo": offered.pay_to,
            "maxAmountRequired": offered.amount,
            "maxTimeoutSeconds": 300,
            "extra": dict(offered.extra or {}),
        }],
    }


def _task_message(status: str, metadata: dict[str, Any], text: str,
                  ) -> dict[str, Any]:
    return {
        "kind": "message",
        "role": "agent",
        "parts": [{"kind": "text", "text": text}],
        "metadata": {STATUS_KEY: status, **metadata},
    }


def _usd_of(required: dict[str, Any]) -> str:
    """Human/machine-readable dollar price from the exact atomic quote (USDC,
    6 decimals) — the REAL on-chain amount, never the credits×CREDIT_USD
    approximation."""
    try:
        atomic = int(required["accepts"][0]["maxAmountRequired"])
        return f"${atomic / 1_000_000:.6f}".rstrip("0").rstrip(".")
    except (KeyError, IndexError, TypeError, ValueError):
        return "$?"


def _free_supply_block(ctx: dict[str, Any]) -> dict[str, Any]:
    """The FREE counts-only supply summary for the asked capability — never
    the paid shortlist, trust scores, verdict, or evidence."""
    return {
        "capability": ctx["capability"],
        "supplied": ctx["supplied"],
        "declared_endpoint": ctx["declared_endpoint"],
        "verified_reachable": ctx["verified_reachable"],
        "demand_id": ctx["demand_id"],
        "demand_recorded_free": True,
    }


def _challenge_text(required: dict[str, Any], ctx: Optional[dict[str, Any]],
                    no_supply: Optional[dict[str, Any]]) -> str:
    """Honest plain-text body of the payment challenge.

    Live-telemetry fix (2026-07-15): a genuine external agent
    (a2a:net:bba57b53…) asked `check: korean-legal` five times and
    `check: fact-check` twice, hit this challenge every time, retried for
    20 minutes, never paid, and downgraded to a free invoke. The honest
    no-supply answer existed but ONLY in task metadata; the text part —
    the one thing every A2A client renders — said nothing but "payment is
    required". A rational agent deciding whether to pay needs, in the text
    itself: the exact price, what the paid read returns, whether any
    candidates even exist (free fact), and the zero-cost alternatives.
    Counts only — the shortlist, scores, verdict and evidence stay paid.
    """
    price = _usd_of(required)
    if no_supply:
        return (
            f"Do NOT pay yet: no supplier of '{no_supply['capability']}' "
            "currently has a VERIFIED reachable endpoint, so this paid trust "
            "read would evaluate an empty candidate set. Your demand is "
            f"already recorded FREE (demand_id {no_supply['demand_id']}). "
            "Free next steps: POST /demand/watch {\"capability\": "
            f"\"{no_supply['capability']}\"}} to be notified when supply "
            "arrives; or register as the first supplier via POST "
            "/agents/register and prove the capability — first competent "
            "supplier of an in-demand capability starts at rank 1. Pull "
            "/demand/feed (free, signed) to see all unmet demand. If you "
            f"still want the paid read it costs {price} USDC on Base via "
            "x402: submit a signed payment payload with this taskId "
            "(x402.payment.status=payment-submitted).")
    supply_note = ""
    if ctx:
        supply_note = (
            f" Free fact: {ctx['verified_reachable']} verified-reachable "
            f"candidate(s) for '{ctx['capability']}' are known; your demand "
            f"is recorded free (demand_id {ctx['demand_id']}).")
    return (
        f"This trust read costs {price} USDC on Base via x402."
        f"{supply_note} Paying returns the full AGD-1 decision: the safest "
        "known agent for the capability, hire/caution/avoid verdict, the "
        "ranked candidates, and a signed offer-receipt. Submit a "
        "signed x402 payment payload with this taskId "
        "(x402.payment.status=payment-submitted). Free alternatives: "
        "'capabilities' (supply/demand map), /demand/feed (signed unmet "
        "demand), or register + prove your own capability (POST "
        "/agents/register).")


def build_payment_required_task(preq: PaidRequest, credits_cost: int,
                                demand_ctx: Optional[dict[str, Any]] = None,
                                ) -> dict[str, Any]:
    """Create + persist a payment task and return the input-required Task."""
    task_id = "x402task_" + uuid.uuid4().hex
    required = payment_required_response(preq, credits_cost)
    store.x402_task_create({
        "id": task_id,
        "status": "payment-required",
        "operation": preq.operation,
        "resource": preq.resource_url,
        "request_hash": preq.request_hash,
        "credits_cost": credits_cost,
        "capability": dict(preq.query).get("capability"),
        "required": required,
        "receipts": [],
        "created_at_epoch": time.time(),
    })
    store.x402_gc_maybe()
    ns = demand_mod.no_supply_block(demand_ctx) if demand_ctx else None
    meta: dict[str, Any] = {REQUIRED_KEY: required}
    if demand_ctx:
        meta["io.agent-guild/supply"] = _free_supply_block(demand_ctx)
    if ns:
        meta["io.agent-guild/no_supply"] = ns
    return {
        "kind": "task",
        "id": task_id,
        "status": {
            "state": "input-required",
            "message": _task_message(
                "payment-required", meta,
                _challenge_text(required, demand_ctx, ns)),
        },
    }


def _extract_payment_meta(message: dict[str, Any]) -> tuple[Optional[str], dict[str, Any]]:
    meta = message.get("metadata") or {}
    return message.get("taskId"), meta


def is_payment_submission(message: dict[str, Any]) -> bool:
    _, meta = _extract_payment_meta(message)
    return meta.get(STATUS_KEY) == "payment-submitted"


def _failed_task(task_id: str, code: str, detail: str,
                 receipts: list[dict[str, Any]]) -> dict[str, Any]:
    settle = {"success": False, "errorReason": detail,
              "network": x402.network(), "transaction": ""}
    return {
        "kind": "task",
        "id": task_id,
        "status": {
            "state": "failed",
            "message": _task_message(
                "payment-failed",
                {ERROR_KEY: code, RECEIPTS_KEY: receipts + [settle]},
                f"Payment failed: {detail}"),
        },
    }


def handle_payment_submission(message: dict[str, Any],
                              caller_did: str = "") -> dict[str, Any]:
    """Settle a submitted A2A payment against its stored quote and return the
    completed (or failed) Task. Idempotent recovery + double-settlement guards
    come from the shared gateway. `caller_did` is the DID of THIS request's
    already-verified caller proof (verified once at the endpoint — the nonce
    is consumed there and never re-verified here); it feeds settlement
    attribution exactly as on HTTP and MCP."""
    task_id, meta = _extract_payment_meta(message)
    if not task_id:
        return _rpc_failure("payment submission missing taskId")
    task = store.x402_task_get(task_id)
    if task is None:
        return _rpc_failure(f"unknown taskId {task_id}")
    receipts = list(task.get("receipts") or [])
    raw_payload = meta.get(PAYLOAD_KEY)
    if not isinstance(raw_payload, dict):
        return _failed_task(task_id, "INVALID_SIGNATURE",
                            "x402.payment.payload missing or malformed",
                            receipts)
    # Rebuild the exact PaidRequest the quote was bound to (never trust the
    # client's echoed resource on the v1 wire — bind to the stored quote).
    preq = _preq_from_task(task)
    credits_cost = int(task.get("credits_cost") or preq.cost)
    # A2A v0.1 payloads are v1-shaped; translate + bind server-side.
    try:
        version = raw_payload.get("x402Version") or raw_payload.get("x402_version")
        if version == 1:
            payload = x402.v1_payload_to_v2(raw_payload, preq, credits_cost)
            protocol = "a2a-v1"
        else:
            payload = PaymentPayload(**raw_payload)
            protocol = "a2a-v2"
    except x402.PaymentBindingError as e:
        return _failed_task(task_id, _err_code(e.reason), e.detail or e.reason,
                            receipts)
    except Exception as e:  # malformed payload
        return _failed_task(task_id, "INVALID_SIGNATURE", str(e)[:200], receipts)
    try:
        settled = payments.settle_x402(payload, preq, protocol=protocol,
                                       caller_did=caller_did)
    except x402.PaymentBindingError as e:
        return _failed_task(task_id, _err_code(e.reason), e.detail or e.reason,
                            receipts)
    except payments.PaymentIdConflict as e:
        return _failed_task(task_id, "DUPLICATE_NONCE", e.detail or e.reason,
                            receipts)
    except payments.CachedPaidResult as cached:
        result = cached.result_json
        settle = cached.settle_record or {}
        return _completed_task(task_id, result, receipts + [
            _settle_response(settle)])
    except payments.PaymentChallenge as e:
        reason = e.body.get("reason") or "SETTLEMENT_FAILED"
        return _failed_task(task_id, _err_code(reason),
                            e.body.get("detail") or reason, receipts)
    # Produce the paid result, bind receipt+evidence to its exact bytes.
    # demand for this request was recorded pre-authorization (B1) when the
    # payment-required task was created — never count it again on payment.
    result = store.check(dict(preq.query).get("capability") or "",
                         demand_recorded=True)
    body = json.dumps(result, default=str).encode("utf-8")
    fin = settled.finalize(body)
    settle_response = _settle_response({
        "success": True,
        "transaction": settled.record.get("transaction"),
        "network": settled.record.get("network"),
        "payer": settled.record.get("payer"),
    }, extensions=fin["extensions"])
    receipts = receipts + [settle_response]
    store.x402_task_update(task_id, status="payment-completed",
                           receipts=receipts,
                           transaction=settled.record.get("transaction"))
    store.record_event(None, "query", ua="a2a/x402", endpoint=preq.operation,
                       paid=True, rail="x402", transport="a2a",
                       network=settled.record.get("network"),
                       x402_protocol=protocol, resource=preq.resource_url)
    return _completed_task(task_id, result, receipts)


def _completed_task(task_id: str, result: Any,
                    receipts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "task",
        "id": task_id,
        "status": {
            "state": "completed",
            "message": _task_message(
                "payment-completed", {RECEIPTS_KEY: receipts},
                "Payment settled. Your trust read is attached."),
        },
        "artifacts": [{
            "artifactId": "trust-read-" + task_id,
            "name": "trust_decision",
            "parts": [{"kind": "text",
                       "text": json.dumps(result, default=str)}],
        }],
    }


def _settle_response(settle: dict[str, Any],
                     extensions: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    out = {
        "success": bool(settle.get("success", settle.get("status") in
                                   ("settled", "settled_confirmed"))),
        "transaction": settle.get("transaction") or "",
        "network": settle.get("network") or x402.network(),
        "payer": settle.get("payer"),
    }
    if extensions:
        out["extensions"] = extensions
    return out


def _preq_from_task(task: dict[str, Any]) -> PaidRequest:
    cap = task.get("capability") or ""
    return payments.check_request(cap)


_ERR_CODES = {
    "amount_mismatch": "INVALID_AMOUNT",
    "recipient_mismatch": "INVALID_AMOUNT",
    "resource_mismatch": "INVALID_AMOUNT",
    "requirements_mismatch": "INVALID_AMOUNT",
    "network_mismatch": "NETWORK_MISMATCH",
    "invalid_network": "NETWORK_MISMATCH",
    "authorization_expired": "EXPIRED_PAYMENT",
    "authorization_not_yet_valid": "EXPIRED_PAYMENT",
    "replay_rejected": "DUPLICATE_NONCE",
    "double_settlement_rejected": "DUPLICATE_NONCE",
    "invalid_payload": "INVALID_SIGNATURE",
    "invalid_x402_version": "INVALID_SIGNATURE",
}


def _err_code(reason: str) -> str:
    return _ERR_CODES.get(reason, "SETTLEMENT_FAILED")


def _rpc_failure(detail: str) -> dict[str, Any]:
    return {"_a2a_x402_error": detail}
