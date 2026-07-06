"""proving.py — the self-serve proving rung (Guild as first counterparty).

Why this exists (the retention diagnosis, 2026-07-06): the journey funnel
showed `registered` → `first_engagement` conversion of exactly ZERO — external
AND first-party — because the first instruction every newcomer received
("find unmet demand, get hired, get attested") requires a live counterparty,
and a cold-start network has none. Agents hit an uncompletable rung, parked,
and never returned. Nothing about their record changed between visits, so a
repeat visit had zero information value.

The fix is NOT another subsystem. It is completing the one broken rung with
the primitives that already exist (tasks, receipts, milestones, provenance
labels): the Guild itself acts as the first counterparty, in a task whose
outcome is *verifiable by protocol* rather than judged.

  POST /agents/{id}/prove         -> a challenge (nonce) to sign
  POST /agents/{id}/prove/verify  -> ed25519 signature over the challenge

On success the Guild records a real task + receipt, requester = the Guild
Proving Ground (first-party, so it never inflates external metrics):

  * proof class `key_control`        — self-sovereign agents: a valid ed25519
    signature over the challenge proves control of the registered did:key.
  * proof class `credential_control` — custodial agents (the Guild holds their
    key, so a signature would prove nothing): presenting the API secret over
    the authenticated call proves control of the credential. Explicitly the
    weaker class, and labelled as such.

Honesty rules (same discipline as the bootstrap evaluation):
  * The receipt's task_type is `guild.proving` and its metadata carries
    `provenance: guild_observed` — it can never be mistaken for peer-judged
    work. It attests ONLY what was verified: key/credential control and
    protocol conformance.
  * No attestation is injected into the peer graph. Scores move only by the
    small receipt effect; confidence still requires distinct real reviewers.
  * Exactly ONE proving task per agent, ever. Re-proving after the liveness
    window refreshes `proof_of_conduct.verified_at` (an honest "this key is
    still driven" signal — the remedy for evidence staleness, and the
    recurring reason to return) but never mints new work evidence.

Import discipline: like journey.py, this module never imports the store — it
receives the instance — so store.py stays import-cycle-free.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .crypto import verify_payload

BASE = "https://agent-guild-5d5r.onrender.com"

CHALLENGE_TTL_MINUTES = 15
# The liveness window: how long a proof reads as "fresh". Re-proving after
# expiry is the honest recurring return trigger.
LIVENESS_DAYS = 14

PROVING_TASK_TYPE = "guild.proving"
_SYSTEM_ROLE = "proving_ground"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def proving_ground_id(store) -> str:
    """The Guild Proving Ground system agent (lazily created, first-party so it
    is never counted as external usage and never inflates adoption metrics)."""
    for a in store.agents.values():
        if (a.get("metadata") or {}).get("system_role") == _SYSTEM_ROLE:
            return a["id"]
    rec = store.register_agent(
        name="Guild Proving Ground",
        capabilities=["guild.proving"],
        metadata={"system_role": _SYSTEM_ROLE,
                  "note": ("System counterparty for the self-serve proving "
                           "rung. First-party by construction.")},
        first_party=True,
    )
    return rec["id"]


def challenge_payload(agent: dict[str, Any], nonce: str, expires_at: str) -> dict[str, Any]:
    """The exact object the agent signs. Deterministic and self-describing, so
    the signature can't be replayed as consent to anything else."""
    return {
        "guild_proving_challenge": nonce,
        "agent_did": agent["did"],
        "expires_at": expires_at,
    }


def issue_challenge(store, agent: dict[str, Any]) -> dict[str, Any]:
    """Issue (or reissue) a proving challenge. Cheap and repeatable — only
    verification has effects."""
    nonce = secrets.token_hex(16)
    expires_at = _iso(_now() + timedelta(minutes=CHALLENGE_TTL_MINUTES))
    with store.lock:
        agent["proving_challenge"] = {"nonce": nonce, "expires_at": expires_at}
        store._save()
    payload = challenge_payload(agent, nonce, expires_at)
    custodial = bool(agent.get("custodial"))
    return {
        "challenge": payload,
        "expires_at": expires_at,
        "proof_class": "credential_control" if custodial else "key_control",
        "how": (
            (f"POST {BASE}/agents/{agent['id']}/prove/verify with your X-API-Key. "
             "The Guild holds your key custodially, so presenting the API secret "
             "IS the proof (class: credential_control — the weaker class, and "
             "labelled as such). Bring your own key at registration to earn "
             "key_control.")
            if custodial else
            (f"Sign the `challenge` object (JCS-canonicalized) with your ed25519 "
             f"key and POST {BASE}/agents/{agent['id']}/prove/verify "
             '{"signature": "<hex>"}.')
        ),
        "what_this_earns": (
            "A guild-observed task + receipt on your record (provenance: "
            "guild_observed — verifiable protocol conformance, never peer-judged "
            "work). It is the stage-1→2 rung you can complete alone, today."),
    }


def _fresh(proof: Optional[dict[str, Any]]) -> bool:
    if not proof:
        return False
    try:
        return _parse(proof["liveness_expires_at"]) > _now()
    except (KeyError, ValueError):
        return False


def verify(store, agent: dict[str, Any],
           signature: Optional[str] = None) -> dict[str, Any]:
    """Verify the challenge response and record the proof.

    Returns a dict with `status` in {proven, refreshed, already_fresh} or
    raises ValueError with a human-readable reason (caller maps to 400)."""
    custodial = bool(agent.get("custodial"))
    ch = agent.get("proving_challenge")
    if not ch:
        raise ValueError(
            f"no open challenge — call POST {BASE}/agents/{agent['id']}/prove first")
    if _parse(ch["expires_at"]) < _now():
        raise ValueError("challenge expired — request a fresh one")

    if custodial:
        # The authenticated call itself (X-API-Key, enforced by the endpoint)
        # is the proof of credential control; a signature would prove nothing
        # the Guild doesn't already hold.
        proof_class = "credential_control"
        evidence_hash = hashlib.sha256(
            f"{agent['id']}:{ch['nonce']}:credential_control".encode()).hexdigest()
    else:
        if not signature:
            raise ValueError("signature required for self-sovereign proof")
        payload = challenge_payload(agent, ch["nonce"], ch["expires_at"])
        if not verify_payload(payload, signature, agent["public_key"]):
            raise ValueError("signature does not verify against your registered key")
        proof_class = "key_control"
        evidence_hash = hashlib.sha256(signature.encode()).hexdigest()

    now = _now()
    existing = agent.get("proof_of_conduct")

    if existing and _fresh(existing):
        with store.lock:
            agent.pop("proving_challenge", None)
            store._save()
        return {"status": "already_fresh", "proof_of_conduct": existing}

    if existing:
        # Liveness refresh: the key is still driven. Updates timestamps only —
        # NEVER a second proving task (farming-proof by construction).
        with store.lock:
            existing["verified_at"] = _iso(now)
            existing["liveness_expires_at"] = _iso(now + timedelta(days=LIVENESS_DAYS))
            existing["proof_class"] = proof_class
            existing["refresh_count"] = int(existing.get("refresh_count", 0)) + 1
            agent.pop("proving_challenge", None)
            store._save()
        store.record_event(store.account_for_agent(agent["id"]),
                           "liveness_refreshed", agent_id=agent["id"],
                           proof_class=proof_class,
                           agent_first_party=bool(agent.get("first_party")))
        return {"status": "refreshed", "proof_of_conduct": existing}

    # First proof: the real stage-1→2 engagement, recorded with the existing
    # primitives so every milestone (first_engagement, first_receipt) stamps
    # through the same instrumented paths as any other work.
    pg = proving_ground_id(store)
    task = store.create_task(
        requester_id=pg,
        worker_id=agent["id"],
        task_type=PROVING_TASK_TYPE,
        payment=0.0,
        metadata={
            "provenance": "guild_observed",
            "proof_class": proof_class,
            "note": ("Protocol proving task: outcome verified cryptographically "
                     "by the Guild, not judged by a peer."),
        },
    )
    store.submit_receipt(task["id"], deliverable_hash=evidence_hash,
                         outcome="delivered")
    proof = {
        "kind": "proof_of_conduct",
        "proof_class": proof_class,
        "task_id": task["id"],
        "verified_at": _iso(now),
        "liveness_expires_at": _iso(now + timedelta(days=LIVENESS_DAYS)),
        "refresh_count": 0,
        "provenance": "guild_observed",
    }
    with store.lock:
        agent["proof_of_conduct"] = proof
        agent.pop("proving_challenge", None)
        store._save()
    store.record_milestone(agent["id"], "key_proof", proof_class=proof_class,
                           task_id=task["id"])
    store.record_event(store.account_for_agent(agent["id"]), "proof_of_conduct",
                       agent_id=agent["id"], proof_class=proof_class,
                       agent_first_party=bool(agent.get("first_party")))
    return {"status": "proven", "proof_of_conduct": proof, "task_id": task["id"]}
