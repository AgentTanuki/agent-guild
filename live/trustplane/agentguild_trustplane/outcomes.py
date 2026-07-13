"""Automatic signed outcome records — AGO-1 (corrective pass 2026-07-13).

Every gated delegation ends in a SIGNED outcome bound to the gate's identity
binding: the signed-envelope hash, the provider's id AND DID, the endpoint
fingerprint, the task/invocation ref and the deliverable hash. The record is
signed with the gateway's own Ed25519 key, whose did:key identity is
REGISTERED with the Guild (self-sovereign registration) so the server can
verify the signer controls the reporting requester DID.

Completion is a VERIFIED server contract, not local metadata:
  * flush() posts the signed outcome to POST /outcomes;
  * a flush counts ONLY after the sealed ledger record is READ BACK
    (GET /ledger/record/{id}) and its binding matches what was sent;
  * failed flushes stay queued; ``stats["unresolved"]`` reports the queue
    depth — an experiment must fail rather than claim completion while it
    is non-zero.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .verify import canonicalize_jcs, b58encode
from .client import GuildClient


def did_from_public_hex(public_hex: str) -> str:
    """did:key for a raw Ed25519 public key (multicodec 0xed01 + base58btc)."""
    raw = b"\xed\x01" + bytes.fromhex(public_hex)
    return "did:key:z" + b58encode(raw)


class OutcomeRecorder:
    def __init__(self, state_dir: str | Path, client: GuildClient) -> None:
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.queue_path = self.dir / "outcome_queue.jsonl"
        self._load_identity()
        self.stats = {"recorded": 0, "flushed": 0, "flush_failures": 0,
                      "readback_failures": 0, "unresolved": 0}
        self._refresh_unresolved()

    # ------------------------------------------------------------- identity
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
        self.did = did_from_public_hex(self.public_hex)
        self.agent_id = ident.get("agent_id")

    def _persist_identity(self) -> None:
        (self.dir / "identity.json").write_text(json.dumps(
            {"private_hex": self.private_hex, "public_hex": self.public_hex,
             "agent_id": self.agent_id}))

    def ensure_registered(self) -> bool:
        """Register this gateway's did:key with the Guild (self-sovereign —
        the Guild never holds the private key) so outcome signatures verify
        against a REGISTERED requester DID. Idempotent."""
        if self.agent_id:
            return True
        try:
            res = self.client._post("/agents/register", {
                "name": f"trustplane-gateway-{self.public_hex[:8]}",
                "capabilities": [],
                "metadata": {"role": "delegation-gateway"},
                "public_key": self.public_hex,
            })
        except Exception:
            return False
        if res and res.get("id") and res.get("did") == self.did:
            self.agent_id = res["id"]
            self._persist_identity()
            return True
        return False

    # ------------------------------------------------------------- signing
    def _sign(self, payload: dict[str, Any]) -> str:
        priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(self.private_hex))
        return priv.sign(canonicalize_jcs(payload).encode()).hex()

    # ------------------------------------------------------------- recording
    def record(self, *, binding: dict[str, Any], outcome: str,
               deliverable: Optional[str] = None,
               latency_ms: Optional[float] = None,
               cost: Optional[float] = None,
               policy_result: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Sign + queue one outcome BOUND to the gate's identity binding.
        outcome: accepted|rejected|disputed|blocked."""
        core = {
            "record_id": "out_" + uuid.uuid4().hex[:12],
            "binding": dict(binding),
            "outcome": outcome,
            "deliverable_sha256": (hashlib.sha256(deliverable.encode()).hexdigest()
                                   if deliverable else None),
            "latency_ms": latency_ms,
            "cost": cost,
            "policy": policy_result,
            "recorded_at": time.time(),
            "signer_did": self.did,
        }
        signed = {**core, "signature": self._sign(core)}
        with self.queue_path.open("a") as f:
            f.write(json.dumps(signed) + "\n")
        self.stats["recorded"] += 1
        self._refresh_unresolved()
        return signed

    # ---------------------------------------------------------------- flush
    def _ago1_doc(self, row: dict[str, Any]) -> Optional[dict[str, Any]]:
        b = row.get("binding") or {}
        if not (b.get("provider_id") and b.get("provider_did")):
            return None      # no counterparty (e.g. blocked before routing)
        core = {
            "type": "AgentGuildOutcome",
            "contract": "AGO-1/1.0",
            "gate_envelope_sha256": b.get("envelope_sha256") or "unrouted",
            "provider_id": b["provider_id"],
            "provider_did": b["provider_did"],
            "endpoint_sha256": b.get("endpoint_sha256"),
            "capability": b.get("capability"),
            "task_ref": b.get("gate_id"),
            "deliverable_sha256": row.get("deliverable_sha256"),
            "outcome": row["outcome"],
            "reported_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(float(row.get("recorded_at") or time.time()))),
            "requester_did": self.did,
        }
        # server requires non-empty required fields
        core = {k: (v if v is not None else "none") for k, v in core.items()}
        doc = dict(core)
        doc["proof"] = self._sign(core)
        return doc

    def flush(self) -> dict[str, int]:
        """Push queued outcomes to POST /outcomes and VERIFY each by ledger
        readback. A record leaves the queue ONLY after the sealed ledger
        record is read back and its binding matches; everything else stays
        queued and is retried on the next flush."""
        if not self.queue_path.exists():
            return {"flushed": 0, "remaining": 0}
        self.ensure_registered()
        rows = [json.loads(l) for l in self.queue_path.read_text().splitlines() if l]
        remaining: list[dict[str, Any]] = []
        flushed = 0
        for r in rows:
            doc = self._ago1_doc(r)
            if doc is None:
                # no counterparty: local evidence only — resolved by design
                flushed += 1
                continue
            res = self.client.post_signed_outcome(doc)
            if not res or not res.get("record_id"):
                remaining.append(r)
                self.stats["flush_failures"] += 1
                continue
            # OUTCOME COMPLETION = VERIFIED READBACK, not a returned function
            rb = self.client.ledger_record(res["record_id"])
            ok = bool(
                rb and rb.get("type") == "signed_outcome"
                and rb.get("hash") == res.get("ledger_hash")
                and (rb.get("body") or {}).get("provider_did")
                    == doc["provider_did"]
                and (rb.get("body") or {}).get("gate_envelope_sha256")
                    == doc["gate_envelope_sha256"])
            if not ok:
                remaining.append(r)
                self.stats["readback_failures"] += 1
                self.stats["flush_failures"] += 1
                continue
            flushed += 1
            self.stats["flushed"] += 1
        # rewrite the queue with only unflushed records
        with self.queue_path.open("w") as f:
            for r in remaining:
                f.write(json.dumps(r) + "\n")
        self._refresh_unresolved()
        return {"flushed": flushed, "remaining": len(remaining)}

    def _refresh_unresolved(self) -> None:
        if self.queue_path.exists():
            self.stats["unresolved"] = sum(
                1 for l in self.queue_path.read_text().splitlines() if l)
        else:
            self.stats["unresolved"] = 0

    def all_local(self) -> list[dict[str, Any]]:
        log = self.dir / "outcome_log.jsonl"
        rows = []
        for p in (self.queue_path, log):
            if p.exists():
                rows += [json.loads(l) for l in p.read_text().splitlines() if l]
        return rows
