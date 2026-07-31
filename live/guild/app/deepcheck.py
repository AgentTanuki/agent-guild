"""Deep preflight and the signed evidence bundle — the paid artefacts.

WHAT THE CUSTOMER IS ACTUALLY BUYING
  Free ``/preflight`` answers "does this endpoint work right now?" — enough to
  avoid the worst mistake, and it stays free forever because a paywall in front
  of that answer would make the ecosystem worse and the index poorer.

  The paid layer answers the questions a caller cannot answer for themselves in
  one request:

    * **history** — has this endpoint drifted? A server that passed a one-off
      review and changed afterwards is invisible to every existing signal, and
      is only visible to someone who kept observing it.
    * **corroboration** — how many independent sources list it, and do their
      claims agree with what we measured?
    * **policy** — an explicit allow / caution / block against a stated
      threshold, so an orchestrator can act on it without writing its own rules.
    * **portability** — a signed bundle the caller keeps, re-verifies offline,
      and can show to a third party. That is the artefact with a reason to be
      paid for: it survives us being unavailable, and it is checkable without
      trusting us at the moment of use.

THE INVARIANT THAT MATTERS MOST
  Paid issuance FAILS CLOSED. If we cannot produce a complete, signed,
  anchored artefact, the caller is not charged and no partial bundle is
  emitted. Selling a degraded evidence object is worse than selling nothing —
  the buyer would rely on it precisely when it is weakest.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .crypto import canonicalize, sign_jcs
from . import preflight, trustindex

#: Default validity of a signed bundle. Short by design: an evidence object
#: about a live endpoint that claims a long life is lying about how fast the
#: world changes.
DEFAULT_TTL_S = 3600


class EvidenceIssuanceRefused(RuntimeError):
    """Paid issuance could not complete. The caller must NOT be charged.

    Raised rather than degrading, because a partial evidence bundle would be
    relied on exactly when it is least reliable."""

    code = "evidence_issuance_refused"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def policy_verdict(observation: dict[str, Any], entry: Optional[dict[str, Any]]
                   ) -> dict[str, Any]:
    """allow / caution / block, with the reason and the threshold stated.

    Deliberately simple and fully explained: a caller must be able to disagree
    with our threshold and apply their own. A verdict whose rule cannot be read
    is a rating agency, not evidence."""
    failed = list(observation.get("failed", []))
    unknowns = list(observation.get("unknowns", []))
    blocking = [f for f in failed
                if f in ("endpoint_reachable", "protocol_handshake")]
    drift = list((entry or {}).get("drift", []))
    recent_drift = drift[-3:]

    if blocking:
        decision, reason = "block", (
            "the endpoint did not prove it can do the thing it is listed for "
            f"({', '.join(blocking)})")
    elif failed:
        decision, reason = "caution", (
            "the endpoint works, but at least one of its own declared claims "
            f"does not hold ({', '.join(failed)})")
    elif len(unknowns) >= 4:
        decision, reason = "caution", (
            f"nothing failed, but {len(unknowns)} of the checks could not be "
            "performed — a clean result over mostly unknowns is thin evidence, "
            "not a clean bill of health")
    else:
        decision, reason = "allow", (
            "every check we could perform passed, and enough of them were "
            "performable for that to mean something")

    if decision == "allow" and len(recent_drift) >= 2:
        decision, reason = "caution", (
            "checks pass right now, but this endpoint has changed state "
            f"{len(recent_drift)} times recently — recent instability is a "
            "risk a single-point-in-time check cannot see")
    return {
        "decision": decision,
        "reason": reason,
        "threshold": (
            "block if a BLOCKING check failed (reachability, protocol "
            "handshake); caution if any declared claim failed, if 4+ checks "
            "were unperformable, or if the endpoint changed state 2+ times "
            "recently; otherwise allow"),
        "caller_note": (
            "This is OUR threshold, stated so you can reject it. `checks` and "
            "`unknowns` are supplied in full precisely so you can apply your "
            "own policy instead."),
        "blocking_failures": blocking,
        "claim_failures": [f for f in failed if f not in blocking],
        "unperformable_checks": unknowns,
        "recent_drift": recent_drift,
    }


def deep_preflight(store: Any, url: str) -> dict[str, Any]:
    """The paid check: live observation + history + corroboration + policy."""
    result = preflight.run(url, store=store)
    fp = trustindex.fingerprint(url)
    entry = (store.trust_index or {}).get(fp)

    sources = [s.get("source") for s in (entry or {}).get("sources", [])]
    declared = (entry or {}).get("declared") or {}
    observed_status = trustindex.status_from_preflight(result)

    corroboration = {
        "independent_sources": len(sources),
        "sources": sources,
        "claim_vs_observation": (
            "no source has listed this endpoint to us — you are the first to "
            "ask about it" if not sources else
            f"{len(sources)} source(s) list this endpoint; we observed it as "
            f"'{observed_status}'"),
        "declared_name": declared.get("name"),
        "declared_capabilities": declared.get("capabilities", []),
    }
    history = {
        "observations": (entry or {}).get("observation_count", 0),
        "first_indexed_at": (entry or {}).get("first_indexed_at"),
        "drift": (entry or {}).get("drift", []),
        "note": ("drift is the state changes we have recorded for this exact "
                 "endpoint. An endpoint with no history is not safe or unsafe "
                 "— it is unobserved, and this is its first data point."),
    }
    return {
        **result,
        "tier": "deep",
        "policy": policy_verdict(result, entry),
        "history": history,
        "corroboration": corroboration,
        "index_status": observed_status,
        "free_tier_note": (
            "GET /preflight (free, no key) returns the live checks and verdict. "
            "This paid tier adds history/drift, cross-source corroboration and "
            "an explicit allow/caution/block policy verdict."),
    }


def evidence_bundle(store: Any, url: str, *, ttl_s: int = DEFAULT_TTL_S,
                    audience: str = "") -> dict[str, Any]:
    """A signed, offline-verifiable snapshot. FAILS CLOSED.

    Anchored to the published checkpoint feed so a holder can prove the
    Guild's state at issuance, and signed with the same did:key that signs
    Agent Passports — one issuer identity, one verification path, no new trust
    root for a customer to learn."""
    deep = deep_preflight(store, url)

    gid = store.guild_identity()
    if not gid.get("did") or not gid.get("private_key"):
        raise EvidenceIssuanceRefused(
            "the Guild signing identity is unavailable; refusing to issue an "
            "unsigned evidence bundle")

    try:
        anchor = store.latest_checkpoint(publish_if_empty=True)
    except Exception as exc:  # noqa: BLE001 — includes the fail-closed 409s
        raise EvidenceIssuanceRefused(
            f"could not anchor the bundle to the canonical ledger: "
            f"{type(exc).__name__}") from exc
    if not anchor or not (anchor.get("checkpoint") or {}).get("head_hash"):
        raise EvidenceIssuanceRefused(
            "no published checkpoint is available to anchor this bundle")

    issued = _now()
    body = {
        "type": "AgentGuildEvidenceBundle",
        "version": 1,
        "subject_endpoint": trustindex.normalise_url(url),
        "subject_id": trustindex.fingerprint(url),
        "audience": audience or None,
        "issued_at": issued.isoformat(),
        "valid_until": (issued + timedelta(
            seconds=max(60, min(int(ttl_s or DEFAULT_TTL_S), 7 * 24 * 3600)))
        ).isoformat(),
        "observation": {
            "verdict": deep.get("verdict"),
            "checks": deep.get("checks", []),
            "failed": deep.get("failed", []),
            "unknowns": deep.get("unknowns", []),
            "method": deep.get("method"),
            "limits": deep.get("limits"),
        },
        "policy": deep.get("policy"),
        "history": deep.get("history"),
        "corroboration": deep.get("corroboration"),
        "issuer": gid["did"],
        "ledger_anchor": {
            "checkpoint_index": anchor.get("index"),
            "head_hash": (anchor.get("checkpoint") or {}).get("head_hash"),
            "published_at": anchor.get("published_at"),
        },
        "verification": {
            "suite": "eddsa-jcs-2022",
            "issuer_did_document": "/.well-known/agent-guild-did.json",
            "how": ("canonicalize the bundle WITHOUT the `proof` field (JCS), "
                    "then verify `proof` as an ed25519 signature over that "
                    "canonical form using the issuer did:key. No call to the "
                    "Guild is required — that is the point of the artefact."),
        },
        "honesty": (
            "This attests to what the Guild OBSERVED at `issued_at`, not to "
            "the future behaviour of the endpoint. `unknowns` are checks that "
            "could not be performed and are excluded from the policy verdict, "
            "never averaged into it."),
    }
    try:
        proof = sign_jcs(body, gid["private_key"])
    except Exception as exc:  # noqa: BLE001
        raise EvidenceIssuanceRefused(
            f"signing failed: {type(exc).__name__}") from exc
    if not proof:
        raise EvidenceIssuanceRefused("signing produced no proof")

    bundle = {**body, "proof": proof}
    bundle["bundle_sha256"] = hashlib.sha256(
        canonicalize(bundle).encode("utf-8")).hexdigest()
    return bundle


def verify_bundle(store: Any, bundle: dict[str, Any]) -> dict[str, Any]:
    """Verify a bundle we (or a historical Guild key) issued. Free.

    Free on purpose: charging to check an artefact we sold would make the
    artefact worth less than we claimed when we sold it."""
    from .crypto import public_key_from_did, verify_jcs

    if not isinstance(bundle, dict) or "proof" not in bundle:
        return {"valid": False, "reason": "not a bundle (no proof)"}
    body = {k: v for k, v in bundle.items()
            if k not in ("proof", "bundle_sha256")}
    issuer = str(bundle.get("issuer") or "")
    known = []
    try:
        known = list(store.guild_did_history())
    except Exception:  # noqa: BLE001
        known = []
    if issuer not in known:
        return {"valid": False, "reason": "issuer is not a Guild key",
                "issuer": issuer}
    try:
        pub = public_key_from_did(issuer)
        ok = verify_jcs(body, str(bundle["proof"]), pub)
    except Exception as exc:  # noqa: BLE001
        return {"valid": False, "reason": f"malformed proof: {type(exc).__name__}"}

    expired = False
    try:
        expired = _now() > datetime.fromisoformat(str(bundle.get("valid_until")))
    except (TypeError, ValueError):
        expired = True
    return {
        "valid": bool(ok) and not expired,
        "signature_valid": bool(ok),
        "expired": expired,
        "issuer": issuer,
        "subject_endpoint": bundle.get("subject_endpoint"),
        "policy_decision": (bundle.get("policy") or {}).get("decision"),
        "issued_at": bundle.get("issued_at"),
        "note": ("an EXPIRED bundle with a valid signature is still proof of "
                 "what was observed at `issued_at` — it is simply no longer "
                 "evidence about now"),
    }
