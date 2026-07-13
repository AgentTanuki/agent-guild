"""Guild client with outage fallback.

Fetches SIGNED decisions (GET /check?signed=true) and passports, verifies
them locally (verify.py), and writes them through the signed cache. When the
Guild is unreachable the client serves the cache; when the cache has nothing
acceptable the ENGINE applies the tier's fail mode. stdlib urllib only —
integrators can vendor this file.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any, Optional

from .cache import SignedDecisionCache

DEFAULT_BASE = "https://agent-guild-5d5r.onrender.com"
UA = "agentguild-trustplane/0.1"


class GuildClient:
    def __init__(self, base_url: str = DEFAULT_BASE,
                 cache: Optional[SignedDecisionCache] = None,
                 timeout: float = 15.0,
                 api_key: Optional[str] = None) -> None:
        self.base = base_url.rstrip("/")
        self.cache = cache
        self.timeout = timeout
        self.api_key = api_key
        self.stats = {"live_fetches": 0, "cache_serves": 0, "outages": 0}

    def _get(self, path: str) -> dict[str, Any]:
        headers = {"User-Agent": UA}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        req = urllib.request.Request(self.base + path, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode())

    def _post(self, path: str, body: dict[str, Any],
              extra_headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
        headers = {"User-Agent": UA, "Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        headers.update(extra_headers or {})
        req = urllib.request.Request(self.base + path,
                                     data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode())

    # -- decisions ------------------------------------------------------------
    def signed_decision(self, capability: str,
                        ttl_seconds: int = 3600) -> tuple[Optional[dict[str, Any]],
                                                          str, Optional[float]]:
        """-> (signed_envelope|None, channel, age_seconds).

        channel: "live" (fetched+verified now), "cache" (served from signed
        cache — envelope may be past valid_until; age says how old), or
        "outage" (nothing verifiable available)."""
        q = urllib.parse.urlencode({"capability": capability, "signed": "true",
                                    "ttl_seconds": ttl_seconds})
        try:
            doc = self._get(f"/check?{q}")
            self.stats["live_fetches"] += 1
            if self.cache is not None:
                self.cache.put("decision", capability, doc)
            return doc, "live", 0.0
        except Exception:
            pass
        if self.cache is not None:
            doc, state, age = self.cache.get("decision", capability)
            if doc is not None:
                self.stats["cache_serves"] += 1
                return doc, "cache", age
        self.stats["outages"] += 1
        return None, "outage", None

    # -- passports -------------------------------------------------------------
    def passport(self, agent_id: str) -> Optional[dict[str, Any]]:
        try:
            doc = self._get(f"/agents/{urllib.parse.quote(agent_id)}/passport")
            if self.cache is not None:
                self.cache.put("passport", agent_id, doc)
            return doc
        except Exception:
            if self.cache is not None:
                doc, _state, _age = self.cache.get("passport", agent_id)
                return doc
            return None

    # -- outcome reporting -----------------------------------------------------
    def record_collaboration(self, body: dict[str, Any]) -> Optional[dict[str, Any]]:
        try:
            return self._post("/collaborations", body)
        except Exception:
            return None
