"""Signed, exact-payment decisions for autonomous wallet clients.

The valuable claim is not merely "this DID has a score".  It is the complete
statement a wallet needs immediately before signing: this exact payee, on this
exact network, for this exact asset/amount/resource, resolved to this machine
identity and passed this explicit policy at this time.

Every result is a short-lived W3C Verifiable Credential with an
``eddsa-jcs-2022`` Data Integrity proof.  A buyer can retain it, verify it
offline, and prove which evidence and thresholds governed the payment.  The
credential never claims future safety and never turns an unbound wallet into a
negative reputation assertion: unknown identity is a fail-closed payment
decision, not misconduct evidence.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

from . import crypto, vc, walletbinding

CONTRACT = "AGPD-1/1.0"
DEFAULT_TTL_S = 300
MAX_TTL_S = 3600
SERVER_MAX_RISK = 32.99
SERVER_MIN_CONFIDENCE = 0.5
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ATOMIC_AMOUNT = re.compile(r"^[0-9]+$")


class PaymentDecisionRefused(ValueError):
    """The request could not be normalized or signed.  Never bill it."""

    code = "payment_decision_refused"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _address(value: Any, label: str) -> str:
    out = str(value or "").strip().lower()
    if not _EVM_ADDRESS.fullmatch(out):
        raise PaymentDecisionRefused(f"{label} must be an exact EVM address")
    return out


def _number(value: Any, label: str, low: float, high: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise PaymentDecisionRefused(f"{label} must be numeric") from exc
    if not low <= out <= high:
        raise PaymentDecisionRefused(f"{label} must be in [{low}, {high}]")
    return out


def normalise_request(request: Any) -> dict[str, Any]:
    """Return the complete result-affecting request in canonical types."""
    if not isinstance(request, dict) or not isinstance(request.get("payment"), dict):
        raise PaymentDecisionRefused("payment object is required")
    raw = request["payment"]
    network = str(raw.get("network") or "").strip()
    if network not in walletbinding.allowed_networks():
        raise PaymentDecisionRefused(
            "network must be an allowed exact CAIP-2 settlement network")
    amount = str(raw.get("amount") or "").strip()
    if (not _ATOMIC_AMOUNT.fullmatch(amount) or len(amount) > 78
            or int(amount) <= 0 or int(amount) >= 2 ** 256):
        raise PaymentDecisionRefused(
            "amount must be a positive atomic-unit integer string")
    resource = str(raw.get("resource") or "").strip()
    parsed = urlsplit(resource)
    if (len(resource) > 2048 or parsed.scheme not in ("http", "https")
            or not parsed.netloc or parsed.username or parsed.password):
        raise PaymentDecisionRefused(
            "resource must be an http(s) URL without embedded credentials")
    scheme = str(raw.get("scheme") or "exact").strip()
    if not scheme or len(scheme) > 64:
        raise PaymentDecisionRefused("scheme must be a non-empty short string")

    capability = str(request.get("capability") or "").strip()
    if len(capability) > 128:
        raise PaymentDecisionRefused("capability is too long")
    raw_policy = request.get("policy") or {}
    if not isinstance(raw_policy, dict):
        raise PaymentDecisionRefused("policy must be an object")
    requested_max_risk = _number(
        raw_policy.get("max_risk", SERVER_MAX_RISK), "max_risk", 0, 100)
    requested_min_confidence = _number(
        raw_policy.get("min_confidence", SERVER_MIN_CONFIDENCE),
        "min_confidence", 0, 1)
    try:
        ttl = int(request.get("ttl_seconds") or DEFAULT_TTL_S)
    except (TypeError, ValueError) as exc:
        raise PaymentDecisionRefused("ttl_seconds must be an integer") from exc
    ttl = max(60, min(ttl, MAX_TTL_S))
    return {
        "payment": {
            "scheme": scheme,
            "network": network,
            "asset": _address(raw.get("asset"), "asset"),
            "amount": amount,
            "pay_to": _address(raw.get("pay_to"), "pay_to"),
            "resource": resource,
        },
        "capability": capability or None,
        "policy": {
            "requested": {
                "max_risk": requested_max_risk,
                "min_confidence": requested_min_confidence,
            },
            # A caller may make the Guild baseline stricter, never weaken it
            # and then market the resulting `allow` as the Guild's threshold.
            "effective": {
                "max_risk": min(requested_max_risk, SERVER_MAX_RISK),
                "min_confidence": max(
                    requested_min_confidence, SERVER_MIN_CONFIDENCE),
            },
            "server_baseline": {
                "max_risk": SERVER_MAX_RISK,
                "min_confidence": SERVER_MIN_CONFIDENCE,
            },
            "rule": (
                "allow only when the exact payee wallet has an active signed "
                "binding to a registered agent, the optional capability "
                "matches, risk is at or below effective.max_risk, and "
                "confidence is at or above effective.min_confidence"),
        },
        "ttl_seconds": ttl,
    }


def _normalised_sha256(normalized: dict[str, Any]) -> str:
    return hashlib.sha256(
        crypto.canonicalize_jcs(normalized).encode("utf-8")).hexdigest()


def request_sha256(request: Any) -> str:
    return _normalised_sha256(normalise_request(request))


def _policy_result(resolution: dict[str, Any], risk: dict[str, Any] | None,
                   request: dict[str, Any],
                   extra_failures: list[str] | None = None
                   ) -> tuple[str, str, list[str]]:
    failures: list[str] = list(extra_failures or [])
    agent = resolution.get("agent") or {}
    if resolution.get("status") != "bound_registered" or not agent:
        failures.append("exact_wallet_not_bound_to_registered_agent")
    capability = request.get("capability")
    if capability and capability not in (agent.get("capabilities") or []):
        failures.append("required_capability_not_advertised")
    if not risk:
        failures.append("risk_evidence_unavailable")
    else:
        effective = request["policy"]["effective"]
        if float(risk.get("risk", 101)) > effective["max_risk"]:
            failures.append("risk_above_threshold")
        if float(risk.get("confidence", -1)) < effective["min_confidence"]:
            failures.append("confidence_below_threshold")
    if failures:
        return "block", "; ".join(failures), failures
    return "allow", (
        "the exact payment wallet is actively bound to the named registered "
        "agent and satisfies the signed policy thresholds"), []


def issue(store: Any, request: Any, *, now: datetime | None = None,
          policy_extension: Callable[..., dict[str, Any]] | None = None
          ) -> dict[str, Any]:
    """Issue one short-lived, exact-payment credential.  Fails closed."""
    normalized = normalise_request(request)
    issued = now or _now()
    payment = normalized["payment"]
    try:
        resolution = walletbinding.resolve_counterparty(
            store, payment["pay_to"], payment["network"])
        agent = resolution.get("agent") or {}
        risk = store.risk_for(str(agent.get("id") or "")) if agent else None
        provenance = (store.provenance_summary(str(agent.get("id") or ""))
                      if agent else None)
        protection = (policy_extension(
            store, normalized, resolution, risk, provenance)
            if policy_extension is not None else None)
        decision, reason, failures = _policy_result(
            resolution, risk, normalized,
            (protection or {}).get("failures"))
        gid = store.guild_identity()
        if not gid.get("did") or not gid.get("private_key"):
            raise PaymentDecisionRefused("Guild signing identity unavailable")
        checkpoint = ((provenance or {}).get("checkpoint")
                      or store.latest_checkpoint(publish_if_empty=False))
        digest = _normalised_sha256(normalized)
        subject_id = (agent.get("did") or
                      f"urn:agent-guild:wallet:{payment['network']}:{payment['pay_to']}")
        unsigned: dict[str, Any] = {
            "@context": vc.VC_CONTEXT_V2,
            "id": "urn:agent-guild:payment-decision:" + hashlib.sha256(
                f"{digest}:{issued.isoformat()}".encode()).hexdigest(),
            "type": ["VerifiableCredential", "AgentGuildPaymentDecision"],
            "issuer": gid["did"],
            "validFrom": issued.isoformat(),
            "validUntil": (issued + timedelta(
                seconds=normalized["ttl_seconds"])).isoformat(),
            "credentialSubject": {
                "id": subject_id,
                "contract": CONTRACT,
                "request_sha256": digest,
                "payment": payment,
                "counterparty": {
                    "resolution_status": resolution.get("status"),
                    "agent": agent or None,
                    "wallet_binding": resolution.get("binding"),
                },
                "risk": risk,
                "provenance": provenance,
                "policy": normalized["policy"],
                "decision": decision,
                "reason": reason,
                "failures": failures,
                "checkpoint": {
                    "index": checkpoint.get("index") if checkpoint else None,
                    "published_at": (checkpoint.get("published_at")
                                     if checkpoint else None),
                    "head_hash": ((checkpoint.get("checkpoint") or {}).get(
                        "head_hash") if checkpoint and checkpoint.get("checkpoint")
                        else checkpoint.get("head_hash") if checkpoint else None),
                },
                **({"protection": protection} if protection else {}),
                "limits": (
                    "This credential attests to the exact evidence and policy "
                    "evaluation at validFrom. It does not guarantee future "
                    "behavior, settlement success, resource quality, or wallet "
                    "ownership after live binding status changes."),
            },
        }
        # The VC helper is intentionally the one issuance path for conforming
        # eddsa-jcs-2022 documents; no legacy hex-proof document is created.
        return vc._add_data_integrity_proof(
            unsigned, gid["did"], gid["private_key"], issued.isoformat())
    except PaymentDecisionRefused:
        raise
    except Exception as exc:
        raise PaymentDecisionRefused(
            f"could not issue complete signed payment decision: "
            f"{type(exc).__name__}") from exc


def verify(decision: Any, *, expected_request: Any | None = None,
           now: datetime | None = None) -> dict[str, Any]:
    """Free offline-style verification, optionally including exact binding."""
    if not isinstance(decision, dict):
        return {"valid": False, "reason": "decision must be an object"}
    signature_valid = vc.verify_credential(decision)
    subject = decision.get("credentialSubject") or {}
    exact = True
    if expected_request is not None:
        try:
            exact = subject.get("request_sha256") == request_sha256(expected_request)
        except PaymentDecisionRefused:
            exact = False
    clock = now or _now()
    try:
        fresh = (datetime.fromisoformat(decision["validFrom"]) <= clock
                 <= datetime.fromisoformat(decision["validUntil"]))
    except (KeyError, TypeError, ValueError):
        fresh = False
    typed = (CONTRACT == subject.get("contract")
             and "AgentGuildPaymentDecision" in (decision.get("type") or []))
    return {
        "valid": bool(signature_valid and fresh and exact and typed),
        "signature_valid": signature_valid,
        "fresh": fresh,
        "exact_request": exact,
        "contract_valid": typed,
        "decision": subject.get("decision"),
    }
