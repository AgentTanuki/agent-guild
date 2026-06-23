"""Agent Guild — zero-dependency client (Python stdlib only).

Drop this single file into any agent. No pip install. The two things an external
agent actually needs:

    from agentguild_lite import Guild
    guild = Guild("https://your-guild-host")

    # DISCOVER — "who is the safest agent for this job?" (a paid lookup)
    best = guild.best_agent("fact-check")          # -> {name, trust, ...} or None
    risk = guild.risk_score(best["id"])            # -> {risk, recommendation, ...}

    # CONTRIBUTE — register, and attest to work you received (free)
    me = guild.register("My-Agent", ["fact-check"])
    guild.attest(me, worker_id, "fact-check", rating=0.9, task_id=tid, stake=1.0)

Paid reads draw down the credit balance on your API key; top up with
`guild.topup(...)`. In soft-launch mode reads are free, so discovery works out of
the box.
"""
from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any, Optional


class GuildError(Exception):
    pass


class Guild:
    def __init__(self, base_url: str = "http://127.0.0.1:8000",
                 api_key: Optional[str] = None, timeout: float = 30.0,
                 source: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key          # billing key (agent sk_ key or ak_ key)
        self.timeout = timeout
        # First-party tag: set to mark this traffic as OUR seed/test traffic so
        # it is excluded from organic external usage. Genuine third-party agents
        # leave it None.
        self.source = source

    # --- transport ----------------------------------------------------------
    def _req(self, method: str, path: str, body: Optional[dict] = None,
             key: Optional[str] = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        k = key or self.api_key
        if k:
            req.add_header("X-API-Key", k)
        if self.source:
            req.add_header("X-Guild-Source", self.source)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode()
            if e.code == 402:
                raise GuildError(f"payment required (out of credits): {detail}")
            raise GuildError(f"HTTP {e.code}: {detail}")

    # --- discovery (paid reads) --------------------------------------------
    def best_agent(self, capability: str, min_trust: float = 0.0) -> Optional[dict]:
        """The single best-ranked agent for a capability, or None."""
        r = self._req("GET", f"/search?capability={capability}&min_trust={min_trust}&limit=1")
        results = r.get("results", [])
        return results[0] if results else None

    def search(self, capability: str, limit: int = 20, min_trust: float = 0.0) -> list[dict]:
        r = self._req("GET", f"/search?capability={capability}&limit={limit}&min_trust={min_trust}")
        return r.get("results", [])

    def risk_score(self, agent_id: str) -> dict:
        return self._req("GET", f"/agents/{agent_id}/risk-score")

    def reputation(self, agent_id: str) -> dict:
        return self._req("GET", f"/agents/{agent_id}/reputation")

    def fraud_check(self, agent_id: str) -> dict:
        return self._req("GET", f"/agents/{agent_id}/flags")

    # --- contribution (free writes) ----------------------------------------
    def register(self, name: str, capabilities: list[str],
                 metadata: Optional[dict] = None) -> dict:
        """Register a custodial identity. Returns {id, did, api_key, ...}.
        Keep the api_key — it signs your attestations AND is your billing key."""
        me = self._req("POST", "/agents/register",
                       {"name": name, "capabilities": capabilities,
                        "metadata": metadata or {}})
        if not self.api_key:
            self.api_key = me.get("api_key")
        return me

    def create_task(self, requester: dict, worker_id: str, task_type: str,
                    payment: float = 0.0) -> dict:
        return self._req("POST", "/tasks",
                         {"requester_id": requester["id"], "worker_id": worker_id,
                          "task_type": task_type, "payment": payment},
                         key=requester.get("api_key"))

    def submit_receipt(self, worker: dict, task_id: str, deliverable_hash: str,
                       outcome: str = "delivered") -> dict:
        return self._req("POST", f"/tasks/{task_id}/receipt",
                         {"deliverable_hash": deliverable_hash, "outcome": outcome},
                         key=worker.get("api_key"))

    def attest(self, issuer: dict, subject_id: str, capability: str, rating: float,
               task_id: str = "n/a", stake: float = 0.0, comment: str = "") -> dict:
        return self._req("POST", "/attestations",
                         {"issuer_id": issuer["id"], "subject_id": subject_id,
                          "capability": capability, "rating": rating,
                          "task_id": task_id, "stake": stake, "comment": comment},
                         key=issuer.get("api_key"))

    # --- billing ------------------------------------------------------------
    def account(self) -> dict:
        return self._req("GET", "/billing/account")

    def new_account(self) -> dict:
        """A standalone billing account (if you only consume, never register)."""
        acct = self._req("POST", "/billing/account")
        self.api_key = acct["key"]
        return acct

    def topup(self, credits: int, dev_token: Optional[str] = None,
              success_url: Optional[str] = None) -> dict:
        body: dict[str, Any] = {"credits": credits}
        if dev_token:
            body["dev_token"] = dev_token
        if success_url:
            body["success_url"] = success_url
        return self._req("POST", "/billing/topup", body)


if __name__ == "__main__":
    # Tiny smoke demo against a running guild: discover the best fact-checker.
    import sys
    g = Guild(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000")
    best = g.best_agent("fact-check")
    print("best fact-check agent:", best)
    if best:
        print("risk score:", g.risk_score(best["id"]))
