"""Automatic signed outcome records.

Every gated delegation ends in a signed outcome record — success or failure —
so evidence completion is a measured property of the gateway, not a favour a
caller may forget. The record is signed locally with the gateway's own
Ed25519 key (its Guild identity), queued durably, and flushed to the Guild's
/collaborations write path (which grades receipts and writes receipt-backed
attestations into the ledger). Offline outcomes queue and flush later —
outage never loses evidence.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .verify import canonicalize_jcs
from .client import GuildClient


class OutcomeRecorder:
    def __init__(self, state_dir: str | Path, client: GuildClient) -> None:
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.queue_path = self.dir / "outcome_queue.jsonl"
        self._load_identity()
        self.stats = {"recorded": 0, "flushed": 0, "flush_failures": 0}

    def _load_identity(self) -> None:
        p = self.dir / "identity.json"
        if p.exists():
            ident = json.loads(p.read_text())
        else:
            priv = Ed25519PrivateKey.generate()
            ident = {"private_hex": priv.private_bytes_raw().hex(),
                     "public_hex": priv.public_key().public_bytes_raw().hex()}
            p.write_text(json.dumps(ident))
        self.private_hex = ident["private_hex"]
        self.public_hex = ident["public_hex"]

    def _sign(self, payload: dict[str, Any]) -> str:
        priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(self.private_hex))
        return priv.sign(canonicalize_jcs(payload).encode()).hex()

    def record(self, *, gate_id: str, capability: str, worker_id: Optional[str],
               outcome: str, deliverable: Optional[str] = None,
               latency_ms: Optional[float] = None,
               cost: Optional[float] = None,
               policy_result: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Sign + queue one outcome. outcome: accepted|rejected|disputed|blocked."""
        core = {
            "record_id": "out_" + uuid.uuid4().hex[:12],
            "gate_id": gate_id,
            "capability": capability,
            "worker_id": worker_id,
            "outcome": outcome,
            "deliverable_sha256": (hashlib.sha256(deliverable.encode()).hexdigest()
                                   if deliverable else None),
            "latency_ms": latency_ms,
            "cost": cost,
            "policy": policy_result,
            "recorded_at": time.time(),
            "signer_public_hex": self.public_hex,
        }
        signed = {**core, "signature": self._sign(core)}
        with self.queue_path.open("a") as f:
            f.write(json.dumps(signed) + "\n")
        self.stats["recorded"] += 1
        return signed

    def flush(self) -> dict[str, int]:
        """Push queued DELEGATION outcomes (accepted/rejected) to the Guild's
        /collaborations write path; blocked delegations stay local (there is
        no counterparty task to grade). Failed pushes stay queued."""
        if not self.queue_path.exists():
            return {"flushed": 0, "remaining": 0}
        rows = [json.loads(l) for l in self.queue_path.read_text().splitlines() if l]
        remaining: list[dict[str, Any]] = []
        flushed = 0
        for r in rows:
            if r["outcome"] not in ("accepted", "rejected", "disputed") or \
               not r.get("worker_id"):
                continue  # blocked/no-counterparty records are local evidence only
            res = self.client.record_collaboration({
                "worker_id": r["worker_id"],
                "capability": r["capability"],
                "outcome": r["outcome"],
                "rating": 0.9 if r["outcome"] == "accepted" else 0.1,
                "deliverable_hash": r.get("deliverable_sha256"),
                "metadata": {"gateway_record_id": r["record_id"],
                             "gateway_signature": r["signature"],
                             "gateway_signer": r["signer_public_hex"]},
            })
            if res is None:
                remaining.append(r)
                self.stats["flush_failures"] += 1
            else:
                flushed += 1
                self.stats["flushed"] += 1
        # rewrite the queue with only unflushed delegation records
        with self.queue_path.open("w") as f:
            for r in remaining:
                f.write(json.dumps(r) + "\n")
        return {"flushed": flushed, "remaining": len(remaining)}

    def all_local(self) -> list[dict[str, Any]]:
        log = self.dir / "outcome_log.jsonl"
        rows = []
        for p in (self.queue_path, log):
            if p.exists():
                rows += [json.loads(l) for l in p.read_text().splitlines() if l]
        return rows
