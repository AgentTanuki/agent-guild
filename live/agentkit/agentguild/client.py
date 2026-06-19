"""Tiny Python client for the Agent Guild API.

Any agent — in any framework — uses this to register an identity, discover
counterparties by capability + reputation, and issue signed attestations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import httpx


@dataclass
class GuildIdentity:
    id: str
    did: str
    public_key: str
    api_key: Optional[str] = None
    capabilities: list[str] = field(default_factory=list)


class GuildClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout)

    # --- identity -----------------------------------------------------------
    def register(
        self,
        name: str,
        capabilities: list[str],
        metadata: Optional[dict[str, Any]] = None,
        public_key: Optional[str] = None,
        seed: bool = False,
        admin_token: Optional[str] = None,
    ) -> GuildIdentity:
        headers = {"X-Admin-Token": admin_token} if admin_token else {}
        r = self._http.post("/agents/register", headers=headers, json={
            "name": name, "capabilities": capabilities,
            "metadata": metadata or {}, "public_key": public_key, "seed": seed,
        })
        r.raise_for_status()
        d = r.json()
        return GuildIdentity(
            id=d["id"], did=d["did"], public_key=d["public_key"],
            api_key=d.get("api_key"), capabilities=d["capabilities"],
        )

    # --- discovery ----------------------------------------------------------
    def search(self, capability: str, limit: int = 20, min_trust: float = 0.0,
               api_key: Optional[str] = None) -> list[dict[str, Any]]:
        # search is a metered read; pass a billing key when enforcement is on.
        headers = {"X-API-Key": api_key} if api_key else {}
        r = self._http.get("/search", headers=headers, params={
            "capability": capability, "limit": limit, "min_trust": min_trust,
        })
        r.raise_for_status()
        return r.json()["results"]

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        r = self._http.get(f"/agents/{agent_id}")
        r.raise_for_status()
        return r.json()

    def reputation(self, agent_id: str) -> dict[str, Any]:
        r = self._http.get(f"/agents/{agent_id}/reputation")
        r.raise_for_status()
        return r.json()

    def best_agent(self, capability: str, min_trust: float = 0.0) -> Optional[dict[str, Any]]:
        results = self.search(capability, limit=1, min_trust=min_trust)
        return results[0] if results else None

    def risk_score(self, agent_id: str) -> dict[str, Any]:
        r = self._http.get(f"/agents/{agent_id}/risk-score")
        r.raise_for_status()
        return r.json()

    # --- tasks / receipts (v0.2) -------------------------------------------
    def create_task(
        self,
        requester: GuildIdentity,
        worker_id: str,
        task_type: str,
        payment: float = 0.0,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        r = self._http.post(
            "/tasks",
            headers={"X-API-Key": requester.api_key or ""},
            json={"requester_id": requester.id, "worker_id": worker_id,
                  "task_type": task_type, "payment": payment,
                  "metadata": metadata or {}},
        )
        r.raise_for_status()
        return r.json()

    def submit_receipt(
        self,
        worker: GuildIdentity,
        task_id: str,
        deliverable_hash: str,
        deliverable_url: Optional[str] = None,
        outcome: str = "delivered",
    ) -> dict[str, Any]:
        r = self._http.post(
            f"/tasks/{task_id}/receipt",
            headers={"X-API-Key": worker.api_key or ""},
            json={"deliverable_hash": deliverable_hash,
                  "deliverable_url": deliverable_url, "outcome": outcome},
        )
        r.raise_for_status()
        return r.json()

    def evidence(self, agent_id: str) -> dict[str, Any]:
        r = self._http.get(f"/agents/{agent_id}/evidence")
        r.raise_for_status()
        return r.json()

    def flags(self, min_suspicion: float = 0.4) -> list[dict[str, Any]]:
        r = self._http.get("/flags", params={"min_suspicion": min_suspicion})
        r.raise_for_status()
        return r.json()["flagged"]

    # --- attestation --------------------------------------------------------
    def attest(
        self,
        issuer: GuildIdentity,
        subject_id: str,
        capability: str,
        rating: float,
        task_id: str = "n/a",
        comment: str = "",
        stake: float = 0.0,
    ) -> dict[str, Any]:
        r = self._http.post(
            "/attestations",
            headers={"X-API-Key": issuer.api_key or ""},
            json={"issuer_id": issuer.id, "subject_id": subject_id,
                  "capability": capability, "rating": rating,
                  "task_id": task_id, "comment": comment, "stake": stake},
        )
        r.raise_for_status()
        return r.json()

    def health(self) -> dict[str, Any]:
        r = self._http.get("/health")
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._http.close()
