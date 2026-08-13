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
            "description": (f"Agent Guild paid read ({preq.operation}): "
                            + operation_label(preq.operation)),
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


#: What each paid operation actually DELIVERS, in the caller's terms. The
#: challenge text is the one thing every A2A client renders, so it is the only
#: place a rational agent can decide whether the price is worth paying. Copy
#: that describes a DIFFERENT product than the one being sold is worse than no
#: copy: the agent evaluates an offer nobody is making, and declines it.
#:
#: Found in production 2026-07-31: a deep-preflight quote was rendered with the
#: capability-read copy ("the safest known agent for the capability"), so a
#: caller asking whether ONE endpoint is safe to pay was quoted for an agent
#: shortlist. That lands squarely on the first blocked boundary — qualified
#: paid-offer exposure — where every impression counts.
OPERATION_COPY: dict[str, str] = {
    "machine_envelope": (
        "Paying returns a privacy-preserving, Guild-signed machine envelope "
        "for the exact payload digest you named: authenticated sender DID, "
        "recipient, message kind, nonce, expiry and any declared economic "
        "terms. The Guild never receives the payload and attests provenance "
        "and integrity — not that the message is true or settled."),
    "payment_decision": (
        "Paying returns a short-lived, Guild-signed AGPD-1 credential for the "
        "exact payment you named: payee, CAIP-2 network, token, atomic amount "
        "and resource, bound to active wallet identity, current risk evidence, "
        "explicit policy thresholds and an allow/block decision. The proof is "
        "portable and verifiable offline before any payment payload is signed."),
    "protected_payment_decision": (
        "Paying returns the higher-assurance AGPD-1 credential for an exact "
        "Base-USDC payment: active wallet identity, current risk, fresh "
        "verified routing and evidence depth appropriate to the exact value "
        "at risk. The transparent fee is 25 basis points of protected value, "
        "with a $0.01 floor and $10,000 ceiling."),
    "deep_preflight": (
        "Paying returns the DEEP endpoint trust check for the exact URL you "
        "named: every live check re-run at request time (does it complete a "
        "real protocol handshake, does its agent card resolve, is that card "
        "signed, does an advertised payment surface actually challenge with "
        "402), PLUS what one request cannot tell you — this endpoint's drift "
        "history with us, cross-source corroboration, and an explicit "
        "allow / caution / block policy verdict whose threshold is published "
        "so you can reject it and apply your own."),
    "evidence_bundle": (
        "Paying returns a SIGNED evidence bundle for the endpoint you named: "
        "the full observation, the policy verdict and a ledger anchor, sealed "
        "with the Guild's did:key so you can keep it and re-verify it offline "
        "later without calling us at all."),
    "watch_cycle": (
        "Paying covers one recheck CYCLE of continuous monitoring for the "
        "endpoint you named. You are charged per recheck actually performed, "
        "so a dormant endpoint costs nothing."),
    "best_agent": (
        "Paying returns the full AGD-1 decision: the safest known agent for "
        "the capability, hire/caution/avoid verdict, the ranked candidates, "
        "and a signed offer-receipt."),
    "signed_decision": (
        "Paying returns a Guild-SIGNED AGD-1 decision for the capability: a "
        "portable, offline-verifiable verdict with a bounded validity window "
        "and a checkpoint pin."),
}

#: Zero-cost alternatives, per operation. A challenge that hides the free path
#: is a dark pattern, and the free path is also how the index gets better.
OPERATION_FREE_ALTERNATIVES: dict[str, str] = {
    "machine_envelope": (
        "Free alternative: create agent-guild/caller-proof/v1 yourself and "
        "send it with the message for direct sender verification, without the "
        "independent Guild issuance timestamp. POST /envelopes/verify is free."),
    "payment_decision": (
        "Free alternative: GET /wallet-binding/resolve for signed exact-wallet "
        "identity only, without the current risk evaluation or transaction-"
        "specific signed decision. Verification of an issued decision is free."),
    "protected_payment_decision": (
        "Free alternatives: GET /wallet-binding/resolve for identity only, "
        "or the ordinary low-cost POST /wallet-binding/decision for routine "
        "payments without the value-tier and fresh-routing gates."),
    "deep_preflight": (
        "Free alternatives: 'preflight: <url>' returns the live checks and "
        "verdict for the same endpoint at no cost; 'index' searches every "
        "endpoint the Guild has already observed."),
    "evidence_bundle": (
        "Free alternatives: 'preflight: <url>' for the live checks, and "
        "POST /evidence/verify to verify any bundle you already hold."),
    "watch_cycle": (
        "Free alternative: re-run 'preflight: <url>' yourself whenever you "
        "need it — the watch exists so you do not have to."),
}

DEFAULT_FREE_ALTERNATIVES = (
    "Free alternatives: 'capabilities' (supply/demand map), /demand/feed "
    "(signed unmet demand), or register + prove your own capability (POST "
    "/agents/register).")


#: SHORT product label for the machine-readable `description` fields. Kept
#: separate from the prose above on purpose: the description travels inside the
#: 402 challenge, and the challenge must never carry the vocabulary of the paid
#: RESULT (shortlist, AGD-1 decision, ranked candidates). A caller must be able
#: to tell what they are buying without any of it leaking before they pay —
#: guarded by tests/test_a2a_x402.py.
OPERATION_LABEL: dict[str, str] = {
    "machine_envelope": ("signed machine message/intent commitment: caller-"
                         "authenticated sender, exact payload digest, recipient, "
                         "nonce and expiry; payload stays private"),
    "payment_decision": ("signed exact-payment wallet decision: payee, chain, "
                         "asset, amount and resource bound to current identity "
                         "and risk evidence before signing"),
    "protected_payment_decision": (
        "value-based exact-payment protection: Base-USDC amount, wallet "
        "identity, current risk, fresh routing and evidence depth in one "
        "signed pre-transfer decision"),
    "deep_preflight": ("deep endpoint trust check for one URL: live checks "
                       "plus drift history, corroboration and an "
                       "allow/caution/block policy verdict"),
    "evidence_bundle": ("signed, offline-verifiable evidence bundle for one "
                        "endpoint, anchored to the published ledger"),
    "watch_cycle": "one recheck cycle of continuous endpoint monitoring",
    "best_agent": "trust read: which agent to hire for a capability",
    "signed_decision": ("signed, offline-verifiable trust decision for a "
                        "capability, with a bounded validity window"),
}


def operation_label(operation: str) -> str:
    """One line naming the product, safe to embed in a 402 challenge."""
    return OPERATION_LABEL.get(operation, OPERATION_LABEL["best_agent"])


def operation_copy(operation: str) -> str:
    """What the payer will actually receive. Falls back to the capability-read
    copy only for operations that ARE capability reads."""
    return OPERATION_COPY.get(operation, OPERATION_COPY["best_agent"])


def _challenge_text(required: dict[str, Any], ctx: Optional[dict[str, Any]],
                    no_supply: Optional[dict[str, Any]],
                    operation: str = "best_agent") -> str:
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
        f"This paid read costs {price} USDC on Base via x402."
        f"{supply_note} {operation_copy(operation)} Submit a "
        "signed x402 payment payload with this taskId "
        "(x402.payment.status=payment-submitted). "
        + OPERATION_FREE_ALTERNATIVES.get(operation, DEFAULT_FREE_ALTERNATIVES))


def build_payment_required_task(preq: PaidRequest, credits_cost: int,
                                demand_ctx: Optional[dict[str, Any]] = None,
                                actor: str = "",
                                ua: str = "",
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
        # The EXACT operation and its canonical parameters. A2A quotes in one
        # message and settles in another, so the operation must survive the
        # round trip; rebuilding it from a default made a deep-preflight
        # challenge settle the wrong operation and return the wrong product.
        # Stored here, on OUR record, and never read back from the submission.
        "operation_params": dict(preq.query),
        # Who was quoted. Recorded at quote time so the settled event is
        # attributable to the same caller under the central attribution rule —
        # an unattributable settlement can never be a customer.
        "actor": actor or "",
        "ua": ua or "",
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
                _challenge_text(required, demand_ctx, ns, preq.operation)),
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
    result = _produce_for(preq, settled, task)
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


def _produce_for(preq: PaidRequest, settled: Any,
                 task: dict[str, Any]) -> dict[str, Any]:
    """Produce the product that was actually paid for.

    Every branch records its own settlement metadata, because "the gateway
    settled" and "money moved on mainnet" are different claims and only the
    second one is revenue."""
    from . import deepcheck
    facts = {
        "settlement_mode": "x402",
        "settlement_confirmed": bool((settled.record or {}).get("confirmed")),
        "settlement_mainnet": bool((settled.record or {}).get("mainnet")),
        "settlement_network": (settled.record or {}).get("network"),
        "settlement_amount_atomic": (settled.record or {}).get("amount_atomic"),
        "settlement_tx": (settled.record or {}).get("transaction"),
        "payer_attribution": payments.effective_payer_attribution(
            settled.record or {}),
        "first_party_payer": (settled.record or {}).get("first_party_payer"),
        "externality_attestation": (settled.record or {}).get(
            "externality_attestation"),
    }
    params = dict(preq.query)
    # The credits QUOTED when this task was created — not today's price. A
    # payment settled late must count as evidence about the price the payer
    # was actually shown.
    quoted = task.get("credits_cost")
    actor = task.get("actor") or "a2a"
    ua = task.get("ua") or "a2a/x402"
    if preq.operation == "deep_preflight":
        url = params.get("url") or ""
        out = deepcheck.deep_preflight(store, url)
        store.record_event(actor, "deep_preflight_run", ua=ua,
                           endpoint="preflight_deep", transport="a2a",
                           target=url, paid=True, price_credits=quoted,
                           verdict=(out.get("policy") or {}).get("decision"),
                           **facts)
        return out
    if preq.operation == "evidence_bundle":
        url = params.get("url") or ""
        out = deepcheck.evidence_bundle(
            store, url, ttl_s=int(params.get("ttl_seconds") or 3600))
        store.record_event(actor, "evidence_bundle_issued", ua=ua,
                           endpoint="evidence_bundle", transport="a2a",
                           target=url, paid=True, price_credits=quoted,
                           **facts)
        return out
    if preq.operation == "best_agent":
        capability = params.get("capability") or ""
        out = store.check(capability, demand_recorded=True)
        # The settled trust read is a completion like any other sold
        # operation. Without this event a mainnet-settled A2A best_agent
        # payment — the ONLY operation the 2026-08-13 qualified external
        # challenges named — was invisible to every revenue read.
        store.record_event(actor, "best_agent_served", ua=ua,
                           endpoint="check", transport="a2a",
                           capability=capability, paid=True,
                           price_credits=quoted, **facts)
        return out
    if preq.operation == "signed_decision":
        capability = params.get("capability") or ""
        out = store.signed_decision(
            capability, ttl_seconds=int(params.get("ttl_seconds") or 3600))
        store.record_event(actor, "signed_decision_issued", ua=ua,
                           endpoint="check_signed", transport="a2a",
                           capability=capability, paid=True,
                           price_credits=quoted, **facts)
        return out
    # Legacy fallback (pre-operation tasks only): serve the unsigned trust
    # read. request_from_stored refuses unknown operations upstream.
    return store.check(params.get("capability") or "", demand_recorded=True)


def _preq_from_task(task: dict[str, Any]) -> PaidRequest:
    """Reconstruct the quoted request from OUR OWN stored task record.

    Only `operation` and `operation_params` are consulted, both written by us
    at quote time. Nothing from the payment submission reaches this function —
    a caller must not be able to steer settlement onto a different operation
    than the one they were quoted."""
    operation = task.get("operation") or "best_agent"
    params = task.get("operation_params")
    if not isinstance(params, dict):
        # legacy tasks quoted before operation_params existed
        params = {"capability": task.get("capability") or ""}
    return payments.request_from_stored(operation, params)


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
