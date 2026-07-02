"""The Canonical Ledger of AI-to-AI Collaboration — reference implementation.

This is the moat primitive. Where the rest of the service answers "who is
trustworthy?", the ledger answers a deeper question: **what actually happened
between agents, and can it be proven?** Reputation is then a *pure derivation* of
this immutable record — reproducible by anyone from the signed entries alone.

Design properties (see docs/LEDGER_ARCHITECTURE.md for the full spec):

  * **Append-only + hash-chained.** Each record commits to the previous record's
    hash, so the sequence is tamper-evident: you cannot rewrite history without
    breaking every link after it.
  * **Provenance-tagged.** Every entry carries *where its trust comes from* — one
    of four classes, strongest to weakest:
        guild_mediated      — the full task lifecycle ran through the Guild, both
                              parties signed, deliverable is content-addressed.
        verifiable_outcome  — the outcome carries independently checkable proof
                              (content-addressed deliverable + payment/stake).
        mutual_attestation  — a participating agent's receipt-backed attestation.
        external_import     — explicitly opt-in, signed by the importer, labelled.
    No signal without provenance; weaker provenance counts less.
  * **Challengeable.** Any entry can be disputed by a party with standing; a
    challenge is itself an immutable ledger entry that downweights the target
    pending resolution. Every signal is contestable.
  * **Checkpointable.** The Guild periodically signs a checkpoint (chain head +
    Merkle root) anyone can pin, so even the Guild cannot silently rewrite the
    past — holders of an old checkpoint detect tampering.

This module is a NON-DESTRUCTIVE projection: it derives a ledger view from the
existing tasks + attestations so the architecture can be proven on real data
without changing how writes happen today. Making the ledger the system of record
(writes append directly) is the migration step — intentionally left for a separate,
signed-off sprint because it is irreversible.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from .crypto import (canonicalize, sign_jcs, verify_jcs, public_key_from_did)

GENESIS = "0" * 64

# Provenance classes, strongest → weakest, with the weight each lends to a derived
# reputation signal. The hierarchy is the whole point: trust is proportional to how
# verifiable the evidence is.
PROVENANCE_WEIGHT = {
    "guild_mediated": 1.0,
    "verifiable_outcome": 0.9,
    "mutual_attestation": 0.6,
    "external_import": 0.2,
}
# A live/open challenge downweights the signal pending resolution. An UPHELD
# challenge does NOT zero the record — that would let adjudicated fraud vanish
# from reputation (a whitewashing subsidy). Instead the record keeps its full
# provenance weight and its outcome is treated as a failure (see success()):
# an upheld challenge is the highest-grade *negative* evidence in the system
# (white paper §6.4). A rejected challenge restores full weight.
CHALLENGE_MULTIPLIER = {"none": 1.0, "open": 0.3, "rejected": 1.0, "upheld": 1.0}


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@dataclass
class CollaborationRecord:
    """One AI-to-AI collaboration and its outcome — the atomic ledger entry."""
    seq: int
    requester_did: str
    worker_did: str
    requester_id: str
    worker_id: str
    capability: str
    task_id: str
    outcome: str                      # accepted | disputed | rejected | delivered
    deliverable_hash: Optional[str]
    payment: float
    stake: float
    provenance: str                   # one of PROVENANCE_WEIGHT
    signers: list[str]                # DIDs that signed (requester/worker)
    evidence: dict[str, Any]          # pointers: attestation_id, etc.
    created_at: str
    prev_hash: str
    challenge_status: str = "none"    # none | open | upheld | rejected
    hash: str = ""                    # content hash (set on seal)
    id: str = ""

    def _body(self) -> dict[str, Any]:
        """Everything the hash commits to — i.e. all fields except hash/id."""
        b = asdict(self)
        b.pop("hash", None)
        b.pop("id", None)
        return b

    def seal(self) -> "CollaborationRecord":
        self.hash = _sha(canonicalize(self._body()))
        self.id = "vcr_" + self.hash[:12]
        return self

    def recompute_hash(self) -> str:
        return _sha(canonicalize(self._body()))

    def success(self) -> int:
        # An upheld challenge converts the record into negative evidence: it
        # counts as a failure at full provenance weight, whatever the original
        # outcome claimed. Adjudicated fault must never be erasable (§6.4).
        if self.challenge_status == "upheld":
            return 0
        return 1 if self.outcome == "accepted" else 0

    def weight(self) -> float:
        return (PROVENANCE_WEIGHT.get(self.provenance, 0.0)
                * CHALLENGE_MULTIPLIER.get(self.challenge_status, 1.0))


# Typed, non-collaboration events the durable chain also carries (stage-1 prep:
# the ledger becomes the write path for ALL evidence, not just settled collabs).
# Legacy collab records carry no "type" key and their hashes are untouched — the
# chain does NOT restart. New event kinds are added here, never removed.
GENERIC_ENTRY_TYPES = (
    "register",        # an identity joined (public fields only — never keys)
    "config_change",   # a declared behavioral-configuration change (§7.3)
    "receipt",         # a task receipt landed (raw event; provenance composed later)
    "attestation",     # an attestation was recorded (body carries credential hash)
    "escrow_event",    # escrow opened / released / refunded / disputed
)


@dataclass
class GenericEntry:
    """One typed, hash-chained event. Same sealing discipline as
    CollaborationRecord: the hash commits to everything except hash/id, and each
    entry commits to the previous entry's hash."""
    seq: int
    type: str                      # one of GENERIC_ENTRY_TYPES
    body: dict[str, Any]           # event payload — public data only
    actor_did: str                 # who caused it ("" if unattributed)
    created_at: str
    prev_hash: str
    hash: str = ""
    id: str = ""

    def _body(self) -> dict[str, Any]:
        b = asdict(self)
        b.pop("hash", None)
        b.pop("id", None)
        return b

    def seal(self) -> "GenericEntry":
        self.hash = _sha(canonicalize(self._body()))
        self.id = "evt_" + self.hash[:12]
        return self

    def recompute_hash(self) -> str:
        return _sha(canonicalize(self._body()))


def entry_from_dict(d: dict[str, Any]):
    """Rehydrate a persisted chain entry: typed dicts become GenericEntry,
    everything else is a legacy/collab CollaborationRecord (no `type` key —
    their historical hashes must remain reproducible byte-for-byte)."""
    if d.get("type") in GENERIC_ENTRY_TYPES:
        return GenericEntry(**d)
    return CollaborationRecord(**d)


@dataclass
class Challenge:
    """An immutable dispute against a record — itself part of the ledger."""
    seq: int
    target_id: str
    challenger_did: str
    grounds: str
    stake: float
    status: str                       # open | upheld | rejected
    created_at: str
    prev_hash: str
    hash: str = ""
    id: str = ""

    def _body(self) -> dict[str, Any]:
        b = asdict(self)
        b.pop("hash", None)
        b.pop("id", None)
        return b

    def seal(self) -> "Challenge":
        self.hash = _sha(canonicalize(self._body()))
        self.id = "chl_" + self.hash[:12]
        return self


class Ledger:
    """Append-only, hash-chained sequence of chain entries (collaboration
    records + typed events) and challenges."""

    def __init__(self) -> None:
        self.records: list[Any] = []  # CollaborationRecord | GenericEntry, in chain order
        self.challenges: list[Challenge] = []
        self._head: str = GENESIS

    # --- writing ----------------------------------------------------------
    def append(self, rec: CollaborationRecord) -> CollaborationRecord:
        rec.seq = len(self.records)
        rec.prev_hash = self._head
        rec.seal()
        self.records.append(rec)
        self._head = rec.hash
        return rec

    def append_entry(self, type: str, body: dict[str, Any], actor_did: str = "",
                     created_at: str = "") -> GenericEntry:
        """Append a typed, non-collaboration event to the same chain."""
        if type not in GENERIC_ENTRY_TYPES:
            raise ValueError(f"unknown entry type: {type}")
        e = GenericEntry(seq=len(self.records), type=type, body=body,
                         actor_did=actor_did, created_at=created_at,
                         prev_hash=self._head).seal()
        self.records.append(e)
        self._head = e.hash
        return e

    def collabs(self) -> list[CollaborationRecord]:
        """Only the collaboration records (reputation derives from these; typed
        events are raw evidence composed at interpretation time)."""
        return [r for r in self.records if isinstance(r, CollaborationRecord)]

    def challenge(self, target_id: str, challenger_did: str, grounds: str,
                  stake: float = 0.0, created_at: str = "") -> Challenge:
        ch = Challenge(
            seq=len(self.challenges), target_id=target_id,
            challenger_did=challenger_did, grounds=grounds, stake=stake,
            status="open", created_at=created_at, prev_hash=self._head,
        ).seal()
        self.challenges.append(ch)
        self._head = ch.hash
        for r in self.records:
            if r.id == target_id and r.challenge_status == "none":
                r.challenge_status = "open"
        return ch

    # --- integrity --------------------------------------------------------
    def verify_chain(self) -> bool:
        """Recompute every hash and linkage; True iff the chain is intact."""
        prev = GENESIS
        for r in self.records:
            if r.prev_hash != prev:
                return False
            if r.recompute_hash() != r.hash:
                return False
            prev = r.hash
        return True

    def merkle_root(self) -> str:
        """A Merkle root over all record hashes — a single commitment to the set."""
        layer = [r.hash for r in self.records] or [GENESIS]
        while len(layer) > 1:
            nxt = []
            for i in range(0, len(layer), 2):
                a = layer[i]
                b = layer[i + 1] if i + 1 < len(layer) else layer[i]
                nxt.append(_sha(a + b))
            layer = nxt
        return layer[0]

    def checkpoint(self) -> dict[str, Any]:
        return {
            "count": len(self.records),
            "head_hash": self._head,
            "merkle_root": self.merkle_root(),
            "chain_valid": self.verify_chain(),
        }

    def signed_checkpoint(self, issuer_did: str, issuer_private_hex: str,
                          created_at: str = "") -> dict[str, Any]:
        """A Guild-signed checkpoint anyone can pin to detect later tampering —
        the canonical-ledger trust anchor, no blockchain required."""
        cp = self.checkpoint()
        cp["issuer"] = issuer_did
        cp["created_at"] = created_at
        cp["proof"] = sign_jcs(cp, issuer_private_hex)
        return cp

    @staticmethod
    def verify_checkpoint(cp: dict[str, Any]) -> bool:
        try:
            proof = cp.get("proof")
            body = {k: v for k, v in cp.items() if k != "proof"}
            return verify_jcs(body, proof, public_key_from_did(cp["issuer"]))
        except (KeyError, ValueError, TypeError):
            return False

    # --- reputation as a pure derivation of the ledger --------------------
    def derive_reputation(self) -> dict[str, dict[str, Any]]:
        """Per-worker reputation computed ONLY from immutable, provenance-weighted,
        non-upheld-challenged records. Reproducible by anyone from the ledger."""
        agg: dict[str, dict[str, Any]] = {}
        for r in self.collabs():
            w = r.weight()
            if w <= 0:
                continue
            a = agg.setdefault(r.worker_id, {
                "worker_id": r.worker_id, "worker_did": r.worker_did,
                "weighted_success": 0.0, "weighted_total": 0.0,
                "records": 0, "by_provenance": {},
            })
            a["weighted_success"] += w * r.success()
            a["weighted_total"] += w
            a["records"] += 1
            a["by_provenance"][r.provenance] = a["by_provenance"].get(r.provenance, 0) + 1
        for a in agg.values():
            a["verifiable_success_rate"] = (
                round(a["weighted_success"] / a["weighted_total"], 4)
                if a["weighted_total"] else None
            )
        return agg

    def stats(self) -> dict[str, Any]:
        by_prov: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for r in self.records:
            if isinstance(r, CollaborationRecord):
                by_prov[r.provenance] = by_prov.get(r.provenance, 0) + 1
                by_type["collab"] = by_type.get("collab", 0) + 1
            else:
                by_type[r.type] = by_type.get(r.type, 0) + 1
        return {
            "records": len(self.records),
            "collaborations": by_type.get("collab", 0),
            "challenges": len(self.challenges),
            "open_challenges": sum(1 for c in self.challenges if c.status == "open"),
            "by_provenance": by_prov,
            "by_type": by_type,
            "chain_valid": self.verify_chain(),
            "head_hash": self._head,
        }

    # --- rehydrate a durable (persisted) chain ----------------------------
    @classmethod
    def from_records(cls, dicts: list[dict[str, Any]]) -> "Ledger":
        """Rebuild a ledger from persisted, already-sealed record dicts (the
        durable chain). Hashes are preserved, so verify_chain re-checks them."""
        led = cls()
        for d in dicts:
            led.records.append(entry_from_dict(d))
        led._head = led.records[-1].hash if led.records else GENESIS
        return led

    # --- non-destructive projection from existing state -------------------
    @classmethod
    def from_store(cls, store: Any) -> "Ledger":
        """Project the current tasks + attestations into a ledger view, so the
        architecture runs on real data. Used to backfill the durable chain once,
        and as a fallback view."""
        ledger = cls()
        att_by_task: dict[str, list[dict[str, Any]]] = {}
        for a in store.attestations:
            att_by_task.setdefault(a.get("task_id") or "", []).append(a)
        graded = [t for t in store.tasks.values()
                  if t.get("outcome") in ("accepted", "disputed", "rejected", "delivered")
                  and t.get("deliverable_hash")]
        graded.sort(key=lambda t: t.get("delivered_at") or t.get("created_at") or "")
        for t in graded:
            ledger.append(build_record_for_task(store, t, att_by_task.get(t["id"], [])))
        return ledger


def build_record_for_task(store: Any, t: dict[str, Any],
                          atts: Optional[list[dict[str, Any]]] = None) -> CollaborationRecord:
    """Construct an UNSEALED collaboration record for one task. Shared by the
    projection and the durable write path so classification is identical in both.
    The caller seals it (via Ledger.append) against the current chain head."""
    if atts is None:
        atts = [a for a in store.attestations if (a.get("task_id") == t["id"])]
    req = store.agents.get(t["requester_agent_id"]) or {}
    wrk = store.agents.get(t["worker_agent_id"]) or {}
    meta = t.get("metadata") or {}
    backed = any(a.get("issuer_id") == t["requester_agent_id"]
                 and a.get("subject_id") == t["worker_agent_id"]
                 and a.get("verified") for a in atts)
    provenance, signers, evidence = _classify(t, req, wrk, backed, atts, meta)
    return CollaborationRecord(
        seq=0,
        requester_did=req.get("did", ""), worker_did=wrk.get("did", ""),
        requester_id=t["requester_agent_id"], worker_id=t["worker_agent_id"],
        capability=t.get("task_type", ""), task_id=t["id"],
        outcome=t.get("outcome", "delivered"),
        deliverable_hash=t.get("deliverable_hash"),
        payment=float(t.get("payment", 0.0) or 0.0),
        stake=float(sum(float(a.get("stake", 0.0) or 0.0) for a in atts)),
        provenance=provenance, signers=signers, evidence=evidence,
        created_at=t.get("delivered_at") or t.get("created_at") or "",
        prev_hash=GENESIS,
    )


def _classify(task, req, wrk, backed, atts, meta):
    """Assign the strongest provenance class the evidence supports."""
    both_registered = bool(req) and bool(wrk)
    content_addressed = bool(task.get("deliverable_hash"))
    paid = float(task.get("payment", 0.0) or 0.0) > 0
    if meta.get("imported") or meta.get("external_import"):
        return "external_import", [], {"import_source": meta.get("import_source")}
    # guild_mediated: full lifecycle through the Guild, both parties known, the
    # receipt is content-addressed, AND a receipt-backed mutual attestation exists.
    if both_registered and content_addressed and backed:
        signers = [d for d in (req.get("did"), wrk.get("did")) if d]
        return "guild_mediated", signers, {
            "attestation_ids": [a["id"] for a in atts if a.get("verified")],
            "receipt": task.get("deliverable_hash"),
        }
    # verifiable_outcome: content-addressed deliverable + economic skin (payment).
    if content_addressed and paid:
        return "verifiable_outcome", [wrk.get("did")] if wrk.get("did") else [], {
            "receipt": task.get("deliverable_hash"),
        }
    # mutual_attestation: a receipt-backed attestation but weaker lifecycle proof.
    if backed:
        return "mutual_attestation", [], {
            "attestation_ids": [a["id"] for a in atts if a.get("verified")],
        }
    # fallback: content-addressed but unbacked → still verifiable_outcome (low end)
    return "verifiable_outcome", [], {"receipt": task.get("deliverable_hash")}
