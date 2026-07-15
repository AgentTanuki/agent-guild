"""The shared paid-operation gateway: ONE semantic operation has ONE price and
ONE enforcement policy, regardless of transport (HTTP, MCP, A2A).

Before this module, only HTTP reads were priced: A2A `check: <capability>`
returned the full AGD-1 decision free, and the MCP tools recorded
``paid=false`` while serving the identical payload. All three transports now
route through :func:`authorize` — the single place that decides whether a
request is (a) settled on the x402 rail, (b) charged in SANDBOX credits
(explicitly labelled ``credits_sandbox``, never revenue), (c) free because
enforcement is off (soft launch / local dev), or (d) refused with a complete
machine-readable payment challenge.

Genuinely free operations never come near this module: registration, evidence
writes (attestations/collaborations/receipts), proving, passports, credential
verification, capability listings, self-reads and the deterministic guest
utilities stay free by design.

Exact-resource binding: every quote/acceptance is bound to a
:class:`PaidRequest` — trusted configured origin, actual method, concrete
path (real agent ids, never ``{id}`` templates) and canonically-encoded
result-affecting query parameters — plus amount/asset/network/recipient and
the EIP-3009 expiry+nonce (app/x402.py `check_binding`).

Official x402 extensions implemented here:
  * ``payment-identifier`` (idempotency): identifiers are persisted across
    restarts and bound to payer + exact request hash + payload fingerprint +
    settlement + result hash. Same id + same request → the same cached result
    with NO second settlement; any mismatch (payer, resource, parameters,
    payment) fails closed with a conflict.
  * ``offer-receipt`` (signed offers + receipts): every 402 carries a
    JWS-signed offer; every served payment returns a JWS-signed receipt plus
    the Guild's namespaced evidence attachment (app/x402_artifacts.py).
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import quote

from x402.extensions.payment_identifier import (
    PAYMENT_IDENTIFIER,
    declare_payment_identifier_extension,
    extract_payment_identifier,
    is_valid_payment_id,
)
from x402.schemas import PaymentPayload, PaymentRequired

from . import billing
from . import x402
from . import x402_artifacts as artifacts
from .billing import PRICING, InsufficientCredits, UnknownAccount
from .crypto import canonicalize_jcs

# ---------------------------------------------------------------------------
# PaidRequest: the exact semantic request a payment is bound to
# ---------------------------------------------------------------------------


def _canon_value(v: Any) -> str:
    """Canonical string form for a query value (deterministic across the
    quote and the acceptance — both are computed by THIS server)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    return str(v)


@dataclass(frozen=True)
class PaidRequest:
    """One concrete priced request. `query` holds the RESULT-AFFECTING
    parameters with their effective (default-applied) values, sorted by key —
    so the same semantic request always canonicalizes to the same URL and any
    mutation of path, query, or agent id changes the binding."""
    operation: str                       # PRICING key (the semantic operation)
    method: str                          # actual HTTP method
    path: str                            # concrete path — real ids, no templates
    query: tuple[tuple[str, str], ...] = ()

    @staticmethod
    def build(operation: str, method: str, path: str,
              params: Optional[dict[str, Any]] = None) -> "PaidRequest":
        q = tuple(sorted((str(k), _canon_value(v))
                         for k, v in (params or {}).items()))
        return PaidRequest(operation=operation, method=method.upper(),
                           path=path, query=q)

    @property
    def canonical_query(self) -> str:
        return "&".join(f"{quote(k, safe='')}={quote(v, safe='')}"
                        for k, v in self.query)

    @property
    def resource_url(self) -> str:
        """Trusted configured origin + concrete path + canonical query.
        NEVER derived from Host/X-Forwarded-* headers."""
        base = x402.public_host() + self.path
        return base + ("?" + self.canonical_query if self.query else "")

    @property
    def request_hash(self) -> str:
        return artifacts.sha256_hex(canonicalize_jcs({
            "operation": self.operation, "method": self.method,
            "resource": self.resource_url}).encode("utf-8"))

    @property
    def cost(self) -> int:
        return PRICING[self.operation]


# Builders for every priced semantic operation. MCP and A2A use these too, so
# one semantic operation canonicalizes to one resource URL on every transport.

def check_request(capability: str, signed: bool = False,
                  ttl_seconds: int = 3600) -> PaidRequest:
    return PaidRequest.build("best_agent", "GET", "/check", {
        "capability": capability, "signed": signed, "ttl_seconds": ttl_seconds})


def search_request(capability: str, limit: int = 20,
                   min_trust: float = 0.0) -> PaidRequest:
    return PaidRequest.build("best_agent", "GET", "/search", {
        "capability": capability, "limit": limit, "min_trust": min_trust})


def reputation_request(agent_id: str) -> PaidRequest:
    return PaidRequest.build("reputation", "GET",
                             f"/agents/{agent_id}/reputation")


def journey_request(agent_id: str) -> PaidRequest:
    return PaidRequest.build("reputation", "GET",
                             f"/agents/{agent_id}/journey")


def evidence_request(agent_id: str) -> PaidRequest:
    return PaidRequest.build("evidence", "GET",
                             f"/agents/{agent_id}/evidence")


def risk_score_request(agent_id: str) -> PaidRequest:
    return PaidRequest.build("risk_score", "GET",
                             f"/agents/{agent_id}/risk-score")


def agent_flags_request(agent_id: str) -> PaidRequest:
    return PaidRequest.build("fraud_check", "GET",
                             f"/agents/{agent_id}/flags")


def flags_request(min_suspicion: float = 0.4) -> PaidRequest:
    return PaidRequest.build("fraud_check", "GET", "/flags",
                             {"min_suspicion": min_suspicion})


# ---------------------------------------------------------------------------
# Gateway outcomes
# ---------------------------------------------------------------------------


class PaymentChallenge(Exception):
    """The request is priced and unpaid: carry the COMPLETE machine-readable
    challenge (v2 PaymentRequired + sandbox instructions) to the caller on
    whatever transport it arrived."""

    def __init__(self, preq: PaidRequest, extra: Optional[dict[str, Any]] = None):
        self.preq = preq
        self.cost = preq.cost
        self.model = challenge_model(preq)
        self.body = x402.payment_required_body(preq, self.cost, model=self.model)
        if extra:
            self.body.update(extra)
        super().__init__("payment required")

    def header_value(self) -> str:
        return x402.payment_required_header_value(self.model)


class PaymentIdConflict(Exception):
    """payment-identifier misuse: same id with a different payer, resource,
    parameters or payment — or a concurrent in-flight duplicate. Fails closed
    (maps to HTTP 409; no settlement is attempted)."""

    def __init__(self, reason: str, detail: str = "",
                 payment_id: str = ""):
        self.reason = reason
        self.detail = detail
        self.payment_id = payment_id
        super().__init__(f"{reason}: {detail}" if detail else reason)


class CachedPaidResult(Exception):
    """Same payment-identifier + same request + same payment: return the
    cached result WITHOUT another settlement (official idempotency
    behaviour)."""

    def __init__(self, record: dict[str, Any]):
        self.record = record
        super().__init__("cached paid result")

    @property
    def result_json(self) -> Any:
        return json.loads(self.record["result_body"])

    @property
    def result_bytes(self) -> bytes:
        return self.record["result_body"].encode("utf-8")

    @property
    def settle_header(self) -> str:
        return self.record.get("settle_header", "")

    @property
    def settle_extensions(self) -> Optional[dict[str, Any]]:
        return self.record.get("settle_extensions")

    @property
    def settle_record(self) -> dict[str, Any]:
        return self.record.get("settlement") or {}


@dataclass
class Settled:
    """A successful x402 settlement, waiting for the response bytes so the
    receipt (+ evidence attachment) can bind to the exact served result."""
    preq: PaidRequest
    record: dict[str, Any]
    protocol: str
    payment_id: Optional[str]
    payload_fingerprint: str
    _finalized: dict[str, Any] = field(default_factory=dict)

    def finalize(self, response_bytes: bytes) -> dict[str, Any]:
        """Issue the signed receipt + evidence attachment for the exact bytes
        served, persist the payment-identifier record (cached result), and
        return {header, extensions, settle_response}."""
        if self._finalized:
            return self._finalized
        from .state import store
        identity = store.guild_identity()
        receipt = artifacts.signed_receipt(identity, artifacts.receipt_payload(
            network=self.record.get("network") or x402.network(),
            resource_url=self.preq.resource_url,
            payer=str(self.record.get("payer") or ""),
            transaction=self.record.get("transaction") or "",
        ))
        checkpoint = _checkpoint_pin(store)
        response_sha = artifacts.sha256_hex(response_bytes)
        pid_sha = (artifacts.sha256_hex(self.payment_id.encode())
                   if self.payment_id else None)
        extensions = {
            artifacts.OFFER_RECEIPT_EXTENSION:
                artifacts.offer_receipt_settle_extension(receipt),
            artifacts.EVIDENCE_EXTENSION: artifacts.evidence_extension(
                identity,
                resource_url=self.preq.resource_url,
                request_hash=self.preq.request_hash,
                response_sha256=response_sha,
                transaction=self.record.get("transaction") or "",
                payer=str(self.record.get("payer") or ""),
                payment_identifier_sha256=pid_sha,
                checkpoint=checkpoint,
            ),
        }
        header = x402.settle_response_header_value(self.record, extensions)
        settle_response = x402.settle_response_model(
            self.record, extensions).model_dump(by_alias=True,
                                                exclude_none=True)
        if self.payment_id:
            store.x402_payment_id_complete(
                self.payment_id,
                result_body=response_bytes.decode("utf-8"),
                result_sha256=response_sha,
                settle_header=header,
                settle_extensions=extensions,
                settlement={k: self.record.get(k) for k in (
                    "transaction", "network", "payer", "recipient",
                    "amount_atomic", "asset", "status", "payment_identity",
                    "mainnet", "confirmed")},
            )
        self._finalized = {"header": header, "extensions": extensions,
                           "settle_response": settle_response,
                           "response_sha256": response_sha}
        return self._finalized


@dataclass
class Authorization:
    """The gateway's verdict for one request."""
    mode: str                      # "x402" | "credits_sandbox" | "free" | "self"
    settled: Optional[Settled] = None
    account: Optional[dict[str, Any]] = None


def _checkpoint_pin(store: Any) -> Optional[dict[str, Any]]:
    try:
        cps = getattr(store, "checkpoints", None) or []
        if not cps:
            return None
        cp = cps[-1]
        return {k: cp.get(k) for k in ("seq", "hash", "published_at", "url")
                if cp.get(k) is not None} or None
    except Exception:
        return None


def challenge_model(preq: PaidRequest) -> PaymentRequired:
    """The full v2 PaymentRequired for one concrete request: exact resource,
    bazaar + payment-identifier declaration + JWS-signed offer."""
    from .state import store
    cost = preq.cost
    extensions: dict[str, Any] = {
        PAYMENT_IDENTIFIER: declare_payment_identifier_extension(),
    }
    if x402.enabled():
        offered = x402.requirements(cost)
        identity = store.guild_identity()
        offer = artifacts.signed_offer(identity, artifacts.offer_payload(
            resource_url=preq.resource_url,
            scheme=offered.scheme,
            network=offered.network,
            asset=offered.asset,
            pay_to=offered.pay_to,
            amount=offered.amount,
        ), accept_index=0)
        extensions[artifacts.OFFER_RECEIPT_EXTENSION] = \
            artifacts.offer_receipt_challenge_extension(identity, [offer])
    return x402.payment_required_model(preq, cost, extensions=extensions)


# ---------------------------------------------------------------------------
# payment-identifier idempotency (official extension semantics)
# ---------------------------------------------------------------------------

_pid_lock = threading.Lock()


def _payload_fingerprint(payload: PaymentPayload) -> str:
    return artifacts.sha256_hex(canonicalize_jcs(
        payload.model_dump(by_alias=True, exclude_none=True)).encode("utf-8"))


def _handle_payment_identifier(payload: PaymentPayload, preq: PaidRequest,
                               ) -> tuple[Optional[str], str]:
    """Extract + enforce the payment-identifier extension. Returns
    (payment_id or None, payload_fingerprint). Raises CachedPaidResult for an
    idempotent replay, PaymentIdConflict for any mismatch or in-flight
    duplicate. Reserves the id (persisted) BEFORE settlement so a crash
    between settlement and serving can never pay twice."""
    from .state import store
    fingerprint = _payload_fingerprint(payload)
    pid = extract_payment_identifier(payload, validate=False)
    if pid is None:
        return None, fingerprint
    if not is_valid_payment_id(pid):
        raise PaymentIdConflict("invalid_payment_identifier",
                                "id must be 16-128 chars of [A-Za-z0-9_-]",
                                payment_id=str(pid)[:128])
    payer = ""
    inner = payload.payload if isinstance(payload.payload, dict) else {}
    auth = inner.get("authorization")
    if isinstance(auth, dict):
        payer = str(auth.get("from") or "").lower()
    with _pid_lock:
        rec = store.x402_payment_id_get(pid)
        if rec is None:
            store.x402_payment_id_reserve(pid, payer=payer,
                                          request_hash=preq.request_hash,
                                          resource=preq.resource_url,
                                          operation=preq.operation,
                                          payload_fingerprint=fingerprint)
            return pid, fingerprint
    # existing record — enforce exact binding before anything else
    if rec.get("payer") != payer:
        raise PaymentIdConflict(
            "payment_identifier_payer_mismatch",
            "this identifier is bound to a different payer", payment_id=pid)
    if rec.get("request_hash") != preq.request_hash:
        raise PaymentIdConflict(
            "payment_identifier_resource_mismatch",
            "this identifier is bound to a different resource/request",
            payment_id=pid)
    if rec.get("payload_fingerprint") != fingerprint:
        raise PaymentIdConflict(
            "payment_identifier_payload_mismatch",
            "same identifier, different payment payload", payment_id=pid)
    if rec.get("status") == "completed":
        raise CachedPaidResult(rec)          # idempotent replay, no settlement
    raise PaymentIdConflict(
        "payment_identifier_in_flight",
        "a request with this identifier is currently being settled; retry "
        "shortly to receive the cached result", payment_id=pid)


# ---------------------------------------------------------------------------
# The x402 settle path (shared by every transport)
# ---------------------------------------------------------------------------


def settle_x402(payload: PaymentPayload, preq: PaidRequest,
                protocol: str = "v2",
                method: Optional[str] = None) -> Settled:
    """Verify + settle one x402 payment against one exact request. Raises:
      PaymentBindingError   — binding/config/replay violation (→ 402)
      PaymentIdConflict     — payment-identifier misuse (→ 409)
      CachedPaidResult      — idempotent replay: serve the cached result
      PaymentChallenge      — settlement failed (rejection wrapped in a fresh
                              challenge so the caller can retry properly)
    Returns a Settled whose .finalize(bytes) issues receipt + evidence and
    completes the payment-identifier record."""
    from .state import store
    cost = preq.cost
    if x402.config_errors():
        raise x402.PaymentBindingError("x402_misconfigured",
                                       "; ".join(x402.config_errors()))
    x402.check_binding(payload, preq, cost, method=method)
    pid, fingerprint = _handle_payment_identifier(payload, preq)

    def _fail_pid() -> None:
        if pid:
            store.x402_payment_id_release(pid)

    # persisted guards + idempotent recovery (all survive restarts):
    #   * an identity that already bought a result can never buy another;
    #   * a mainnet settlement that could not be INDEPENDENTLY confirmed
    #     (status settled_unconfirmed) may be RE-PRESENTED: confirmation is
    #     re-run against the recorded tx — the payer is never charged twice
    #     and never loses a paid-but-unconfirmed result to a transient RPC
    #     outage.
    inner = payload.payload if isinstance(payload.payload, dict) else {}
    auth = inner.get("authorization")
    ident = (x402.replay_guard.identity(auth) if isinstance(auth, dict) else "")
    prior = store.x402_latest_for_identity(ident) if ident else None
    if prior and prior.get("status") in store._X402_SERVED_STATUSES:
        _fail_pid()
        raise PaymentChallenge(preq, extra={
            "error": "x402_payment_rejected",
            "reason": "double_settlement_rejected",
            "detail": "this payment identity was already settled"})
    if prior and prior.get("status") == "settled_unconfirmed":
        conf = x402.x402_confirm.confirm_settlement(
            prior.get("transaction") or "",
            asset=prior.get("asset") or "",
            recipient=prior.get("recipient") or "",
            amount_atomic=prior.get("amount_atomic") or "0")
        if not conf.get("confirmed"):
            _fail_pid()
            raise PaymentChallenge(preq, extra={
                "error": "x402_payment_rejected",
                "reason": "settlement_unconfirmed",
                "transaction": prior.get("transaction"),
                "detail": ("settlement is not independently confirmed "
                           "on-chain yet: " + str(conf.get("reason")))[:300]})
        settled = {**prior, "ok": True, "status": "settled_confirmed",
                   "confirmed": True,
                   "confirmation": {k: conf.get(k) for k in
                                    ("confirmed", "reason", "block_number")}}
    else:
        try:
            settled = x402.process_payment(payload, preq, cost,
                                           method=method, protocol=protocol)
        except x402.PaymentBindingError:
            _fail_pid()
            raise
        if settled.get("status") == "settled_unconfirmed":
            # the facilitator claims settlement but the chain does not (yet)
            # prove it: record for recovery/reconciliation, serve NOTHING.
            store.record_x402_payment(preq.operation, cost, settled)
            _fail_pid()
            raise PaymentChallenge(preq, extra={
                "error": "x402_payment_rejected",
                "reason": "settlement_unconfirmed",
                "transaction": settled.get("transaction"),
                "detail": "settlement is not independently confirmed "
                          "on-chain; re-present the same payment to retry "
                          "confirmation — you will not be charged twice"})
        if not settled.get("ok"):
            _fail_pid()
            raise PaymentChallenge(preq, extra={
                "error": "x402_payment_rejected", "settlement": settled})
        # one on-chain transaction can never buy two results
        if store.x402_transaction_served(settled.get("transaction") or ""):
            store.record_x402_payment(
                preq.operation, cost,
                {**settled, "ok": False,
                 "status": "duplicate_transaction_rejected"})
            _fail_pid()
            raise PaymentChallenge(preq, extra={
                "error": "x402_payment_rejected",
                "reason": "duplicate_transaction",
                "detail": "this transaction hash already settled a "
                          "previous request"})
    store.record_x402_payment(preq.operation, cost, settled)
    return Settled(preq=preq, record=settled, protocol=protocol,
                   payment_id=pid, payload_fingerprint=fingerprint)


# ---------------------------------------------------------------------------
# The ONE enforcement policy, transport-agnostic
# ---------------------------------------------------------------------------


def authorize(preq: PaidRequest, *,
              api_key: Optional[str] = None,
              payment: Optional[PaymentPayload] = None,
              protocol: str = "v2",
              ua: str = "",
              transport: str = "http",
              actor: Optional[str] = None) -> Authorization:
    """Authorize one priced request. Order of precedence:

      1. an x402 payment payload  → verify + settle (the real rail);
      2. an API key               → charge SANDBOX credits (never revenue);
      3. enforcement off          → free (soft launch / local dev);
      4. otherwise                → PaymentChallenge (the complete
                                    machine-readable 402, same on every
                                    transport).
    """
    from .state import store
    cost = preq.cost
    if payment is not None and x402.enabled():
        settled = settle_x402(payment, preq, protocol=protocol)
        store.record_event(None, "query", ua=ua, endpoint=preq.operation,
                           paid=True, rail="x402", transport=transport,
                           network=settled.record.get("network"),
                           x402_protocol=protocol,
                           resource=preq.resource_url)
        return Authorization(mode="x402", settled=settled)
    if payment is not None and not x402.enabled():
        raise PaymentChallenge(preq, extra={
            "error": "x402_payment_invalid",
            "reason": "x402_disabled",
            "detail": "the x402 rail is not active on this deployment"})
    if api_key:
        try:
            acct = store.charge(api_key, cost, preq.operation)
        except UnknownAccount:
            if billing.billing_enforced():
                raise PaymentChallenge(preq, extra={
                    "error": "unknown_billing_key",
                    "detail": "unknown billing key (POST /billing/trial for "
                              "a free starter)"})
            store.record_event(api_key, "query", ua=ua,
                               endpoint=preq.operation, paid=False,
                               transport=transport)
            return Authorization(mode="free")
        except InsufficientCredits as e:
            raise PaymentChallenge(preq, extra={
                "error": "insufficient_credits", "balance": e.balance,
                "cost": e.cost, "acquire": acquire_info()})
        store.record_event(api_key, "query", ua=ua, endpoint=preq.operation,
                           paid=True, rail="credits_sandbox",
                           transport=transport)
        owner = acct.get("owner_agent_id")
        if owner:
            store.activate_referral(owner)
        return Authorization(mode="credits_sandbox", account=acct)
    if billing.billing_enforced():
        raise PaymentChallenge(preq, extra={
            "error": "payment_required",
            "detail": "pay via x402 v2 (PAYMENT-SIGNATURE header / "
                      "A2A x402 extension / MCP x402 flow; see the "
                      "PAYMENT-REQUIRED challenge) or present a funded "
                      "X-API-Key (sandbox credits)",
            "cost": cost, "acquire": acquire_info()})
    store.record_event(actor, "query", ua=ua, endpoint=preq.operation,
                       paid=False, transport=transport)
    return Authorization(mode="free")


def acquire_info() -> dict[str, Any]:
    """Machine-readable description of how an agent acquires payment power,
    no human."""
    return {
        "trial": {"method": "POST", "path": "/billing/trial",
                  "human_free": True,
                  "unit": "credits_sandbox (NOT money)"},
        "topup": {"method": "POST", "path": "/billing/topup"},
        "x402": ("active (v2) — retry with a PAYMENT-SIGNATURE header built "
                 "from the PAYMENT-REQUIRED challenge (see `accepts`); "
                 "A2A: x402 extension v0.1 at POST /a2a; MCP: retry the tool "
                 "with _meta['x402/payment']"
                 if x402.enabled() else
                 "protocol supported; rail awaiting a configured treasury"),
        "credit_usd": billing.CREDIT_USD,
    }


# Convenience used by tests + the canary: what a paid transport reply looks
# like for a cached idempotent replay.
def cached_reply_headers(cached: CachedPaidResult) -> dict[str, str]:
    hdrs = {"X-Guild-Payment-Idempotent-Replay": "true"}
    if cached.settle_header:
        hdrs[x402.PAYMENT_RESPONSE_HEADER] = cached.settle_header
    return hdrs
