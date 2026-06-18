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
    def search(self, capability: str, limit: int = 20, min_trust: float = 0.0) -> list[dict[str, Any]]:
        r = self._http.get("/search", params={
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

    # --- attestation --------------------------------------------------------
    def attest(
        self,
        issuer: GuildIdentity,
        subject_id: str,
        capability: str,
        rating: float,
        task_id: str = "n/a",
        comment: str = "",
    ) -> dict[str, Any]:
        r = self._http.post(
            "/attestations",
            headers={"X-API-Key": issuer.api_key or ""},
            json={"issuer_id": issuer.id, "subject_id": subject_id,
                  "capability": capability, "rating": rating,
                  "task_id": task_id, "comment": comment},
        )
        r.raise_for_status()
        return r.json()

    def health(self) -> dict[str, Any]:
        r = self._http.get("/health")
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._http.close()
