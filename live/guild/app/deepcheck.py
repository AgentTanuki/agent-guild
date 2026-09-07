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
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .crypto import sign_jcs
from .artifacts.agentguild_verify import evidence_canonical, verify_evidence_bundle
from . import preflight, trustindex

#: Default validity of a signed bundle. Short by design: an evidence object
#: about a live endpoint that claims a long life is lying about how fast the
#: world changes.
DEFAULT_TTL_S = 3600
MIN_TTL_S = 60
MAX_TTL_S = 7 * 24 * 3600


def normalize_evidence_ttl(ttl_s: int | None) -> int:
    """Return the exact lifetime used by both quote and signed artifact."""
    requested = int(ttl_s or DEFAULT_TTL_S)
    return max(MIN_TTL_S, min(requested, MAX_TTL_S))


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
        "note": ("drift is the state changes recorded for this normalized index "
                 "endpoint group; query strings and trailing slashes may be merged. "
                 "An endpoint with no history is not safe or unsafe "
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
        "version": 2,
        "requested_endpoint": url.strip(),
        "subject_endpoint": trustindex.normalise_url(url),
        "subject_id": trustindex.fingerprint(url),
        "audience": audience or None,
        "issued_at": issued.isoformat(),
        "valid_until": (issued + timedelta(
            seconds=normalize_evidence_ttl(ttl_s))).isoformat(),
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
        # Keep the salt PRIVATE in this bundle. Public ledger observers cannot
        # dictionary-guess a known URL/audience from the published digest.
        "commitment_nonce": secrets.token_hex(16),
        "verification": {
            "suite": "Ed25519",
            "canonicalization": "AGI-1 canonical JSON; ASCII field names and safe integer numbers",
            "issuer_did_document": "/.well-known/agent-guild-did.json",
            "python_sdk": "/sdk/agentguild_verify.py",
            "javascript_sdk": "/sdk/agentguild_verify.mjs",
            "how": ("Use verify_evidence_bundle / verifyEvidenceBundle with a trusted "
                    "expected issuer and the exact requested endpoint/audience. For "
                    "the raw Ed25519 hex signature, canonicalize without BOTH `proof` "
                    "and `bundle_sha256`. The checksum excludes only `bundle_sha256`. "
                    "Verify the salted snapshot commitment, ledger record, Merkle path, "
                    "checkpoint signature and feed-entry signature. No network is needed."),
            "endpoint_scope": ("requested_endpoint is the exact request URL. "
                               "subject_endpoint/subject_id group index history and may "
                               "merge query strings or trailing slashes."),
            "trust_limits": ("Timestamps are issuer assertions, not external time proofs. "
                             "This bundle alone cannot detect an issuer's split ledger views; "
                             "independently retain and compare public checkpoints when needed."),
        },
        "honesty": (
            "This attests to what the Guild OBSERVED at `issued_at`, not to "
            "the future behaviour of the endpoint. `unknowns` are checks that "
            "could not be performed and are excluded from the policy verdict, "
            "never averaged into it."),
    }
    try:
        digest = hashlib.sha256(evidence_canonical(body).encode("utf-8")).hexdigest()
        # Publish ONLY the commitment, never the request, audience or results.
        # An issuance/settlement failure may leave an unpurchased commitment;
        # this event is evidence of issuance, never payment or customer demand.
        with store.lock:
            if store.guild_identity().get("did") != gid["did"]:
                raise EvidenceIssuanceRefused("issuer changed during observation; retry")
            record = store.append_ledger_event(
                "evidence_commitment", {"version": 2, "snapshot_sha256": digest},
                actor_did=gid["did"])
            anchor = store.publish_checkpoint()
            inclusion = store.ledger_inclusion_proof(record["id"], anchor["index"])
        body["ledger_anchor"] = {
            "checkpoint_index": anchor["index"],
            "head_hash": anchor["checkpoint"]["head_hash"],
            "published_at": anchor["published_at"],
            "snapshot_sha256": digest,
            "checkpoint_entry": anchor,
            "inclusion": inclusion,
        }
        evidence_canonical(body)
        proof = sign_jcs(body, gid["private_key"])
    except Exception as exc:  # noqa: BLE001
        raise EvidenceIssuanceRefused(
            f"commitment, anchoring or signing failed: {type(exc).__name__}") from exc
    if not proof:
        raise EvidenceIssuanceRefused("signing produced no proof")

    bundle = {**body, "proof": proof}
    bundle["bundle_sha256"] = hashlib.sha256(
        evidence_canonical(bundle).encode("utf-8")).hexdigest()
    if not verify_evidence_bundle(bundle, expected_issuer=gid["did"],
                                  expected_endpoint=url.strip(), now=_now())["valid"]:
        raise EvidenceIssuanceRefused("produced evidence failed independent artifact validation")
    return bundle


def verify_bundle(store: Any, bundle: dict[str, Any]) -> dict[str, Any]:
    """Verify a bundle we (or a historical Guild key) issued. Free.

    Free on purpose: charging to check an artefact we sold would make the
    artefact worth less than we claimed when we sold it."""
    if not isinstance(bundle, dict):
        return {"valid": False, "reason": "not a bundle"}
    issuer = str(bundle.get("issuer") or "")
    try:
        known = list(store.guild_did_history())
    except Exception:
        known = []
    if issuer not in known:
        return {"valid": False, "reason": "issuer is not a Guild key", "issuer": issuer}
    return verify_evidence_bundle(bundle, expected_issuer=issuer, now=_now())
