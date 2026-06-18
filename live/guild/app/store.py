"""In-memory store with JSON-file persistence.

Local-first: no database required. Set GUILD_DATA to choose the file. Holds
agents (including custodial private keys + api keys — local trust service) and
attestations, and computes reputation on demand with light caching.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from .crypto import generate_keypair, did_from_public_key
from .vc import issue_credential, verify_credential
from .reputation import score_agents, AgentScore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.environ.get("GUILD_DATA", "")
        self.lock = threading.RLock()
        self.agents: dict[str, dict[str, Any]] = {}
        self.attestations: list[dict[str, Any]] = []
        self._rep_cache: Optional[dict[str, AgentScore]] = None
        self._load()

    # --- persistence --------------------------------------------------------
    def _load(self) -> None:
        if self.path and os.path.exists(self.path):
            with open(self.path, "r") as f:
                data = json.load(f)
            self.agents = data.get("agents", {})
            self.attestations = data.get("attestations", [])

    def _save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"agents": self.agents, "attestations": self.attestations}, f, indent=2)
        os.replace(tmp, self.path)

    # --- agents -------------------------------------------------------------
    def register_agent(
        self,
        name: str,
        capabilities: list[str],
        metadata: dict[str, Any],
        public_key: Optional[str] = None,
        seed: bool = False,
    ) -> dict[str, Any]:
        with self.lock:
            agent_id = "agent_" + secrets.token_hex(6)
            if public_key:  # self-sovereign: agent holds its own key
                priv = None
                pub = public_key
                api_key = None
                custodial = False
            else:  # custodial: Guild generates and holds the key
                priv, pub = generate_keypair()
                api_key = "sk_" + secrets.token_hex(24)
                custodial = True
            did = did_from_public_key(pub)
            rec = {
                "id": agent_id,
                "did": did,
                "name": name,
                "capabilities": capabilities,
                "metadata": metadata,
                "public_key": pub,
                "private_key": priv,   # secret; custodial only
                "api_key": api_key,    # secret; custodial only
                "custodial": custodial,
                "seed": bool(seed),
                "created_at": _now(),
            }
            self.agents[agent_id] = rec
            self._rep_cache = None
            self._save()
            return rec

    def get_agent(self, agent_id: str) -> Optional[dict[str, Any]]:
        return self.agents.get(agent_id)

    def agent_by_did(self, did: str) -> Optional[dict[str, Any]]:
        for a in self.agents.values():
            if a["did"] == did:
                return a
        return None

    def seeds(self) -> list[str]:
        return [a["id"] for a in self.agents.values() if a.get("seed")]

    # --- attestations -------------------------------------------------------
    def add_custodial_attestation(
        self,
        issuer: dict[str, Any],
        subject: dict[str, Any],
        capability: str,
        rating: float,
        task_id: str,
        comment: str,
    ) -> dict[str, Any]:
        with self.lock:
            att_id = "att_" + secrets.token_hex(8)
            cred = issue_credential(
                cred_id=f"urn:att:{att_id}",
                types=["WorkAttestation"],
                issuer_did=issuer["did"],
                issuer_private_hex=issuer["private_key"],
                subject_did=subject["did"],
                capability=capability,
                rating=rating,
                task_id=task_id,
                comment=comment,
            )
            return self._store_attestation(att_id, issuer["id"], subject["id"], capability, rating, cred)

    def add_signed_attestation(self, credential: dict[str, Any]) -> dict[str, Any]:
        """Self-sovereign path: verify a pre-signed VC, then store it."""
        with self.lock:
            issuer = self.agent_by_did(credential.get("issuer", ""))
            subj_did = credential.get("credentialSubject", {}).get("id", "")
            subject = self.agent_by_did(subj_did)
            if not issuer or not subject:
                raise ValueError("issuer or subject DID is not a registered agent")
            cs = credential.get("credentialSubject", {})
            att_id = "att_" + secrets.token_hex(8)
            return self._store_attestation(
                att_id, issuer["id"], subject["id"],
                cs.get("capability", ""), float(cs.get("rating", 0.0)), credential,
            )

    def _store_attestation(
        self, att_id, issuer_id, subject_id, capability, rating, cred
    ) -> dict[str, Any]:
        verified = verify_credential(cred)
        rec = {
            "id": att_id,
            "issuer_id": issuer_id,
            "subject_id": subject_id,
            "capability": capability,
            "rating": float(rating),
            "verified": verified,
            "credential": cred,
            "created_at": _now(),
        }
        self.attestations.append(rec)
        self._rep_cache = None
        self._save()
        return rec

    def attestations_for(self, subject_id: str) -> list[dict[str, Any]]:
        return [a for a in self.attestations if a["subject_id"] == subject_id]

    def count_issued(self, issuer_id: str) -> int:
        return sum(1 for a in self.attestations if a["issuer_id"] == issuer_id)

    # --- reputation ---------------------------------------------------------
    def reputation(self) -> dict[str, AgentScore]:
        with self.lock:
            if self._rep_cache is None:
                ids = list(self.agents.keys())
                edges = [
                    (a["issuer_id"], a["subject_id"], a["rating"])
                    for a in self.attestations
                    if a["verified"]
                ]
                self._rep_cache = score_agents(ids, edges, self.seeds()).scores
            return self._rep_cache
