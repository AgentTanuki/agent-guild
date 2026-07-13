"""Guild client with outage fallback — VERIFY BEFORE USE.

Fetches SIGNED decisions (GET /check?signed=true) and passports. Corrective
pass 2026-07-13: a live document is returned as channel="live" ONLY after it

  1. cryptographically verifies (eddsa-jcs-2022 against the issuer did:key),
  2. comes from an allowed/pinned issuer — a changed issuer is accepted only
     via a VERIFIED dual-signed rotation chain fetched from /ledger/rotations,
  3. is inside its validity window,
  4. is AGD-1 conformant (when a decision is present), and
  5. satisfies the one-counterparty binding (decision == routed provider).

A verification failure is an UNVERIFIED state (channel="unverified"), never
"live": the cache is consulted, and if nothing verifiable exists the gateway
fails according to policy (enforce mode denies). stdlib urllib only —
integrators can vendor this file.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Optional

from .cache import SignedDecisionCache
from .contract import validate_decision, binding_violations
from .verify import verify_data_integrity, within_validity

DEFAULT_BASE = "https://agent-guild-5d5r.onrender.com"
UA = "agentguild-trustplane/0.2"


class GuildClient:
    def __init__(self, base_url: str = DEFAULT_BASE,
                 cache: Optional[SignedDecisionCache] = None,
                 timeout: float = 15.0,
                 api_key: Optional[str] = None) -> None:
        self.base = base_url.rstrip("/")
        self.cache = cache
        self.timeout = timeout
        self.api_key = api_key
        # in-process pin fallback when no cache directory is configured
        self._local_pins: list[str] = []
        self.stats = {"live_fetches": 0, "cache_serves": 0, "outages": 0,
                      "live_verify_failures": 0}
        self.last_verify_failure: Optional[str] = None

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

    # -- issuer acceptance -----------------------------------------------------
    def _issuer_allowed(self, issuer_did: str) -> bool:
        """Pinned/TOFU acceptance, with a verified-rotation-chain path for a
        changed issuer. Never accepts an unproven issuer change."""
        if self.cache is not None:
            if self.cache.issuer_ok(issuer_did):
                return True
            # unknown issuer: try to prove continuity via the rotation chain
            try:
                rot = self._get("/ledger/rotations").get("rotations") or []
            except Exception:
                return False
            return self.cache.accept_rotation(issuer_did, rot)
        # no cache: in-process TOFU (still never silently re-pins)
        if not self._local_pins:
            self._local_pins.append(issuer_did)
            return True
        if issuer_did in self._local_pins:
            return True
        try:
            rot = self._get("/ledger/rotations").get("rotations") or []
        except Exception:
            return False
        from .verify import verify_rotation_chain
        for pinned in self._local_pins:
            if verify_rotation_chain(pinned, issuer_did, rot):
                self._local_pins.append(issuer_did)
                return True
        return False

    def _verify_live(self, doc: dict[str, Any]) -> Optional[str]:
        """Full verification of a live signed envelope. Returns None when the
        document is acceptable, else a failure reason."""
        v = verify_data_integrity(doc)
        if not v["verified"]:
            return f"proof: {v['reason']}"
        if not self._issuer_allowed(v["issuer_did"] or ""):
            return f"issuer not allowed: {v['issuer_did']}"
        valid, _age = within_validity(doc)
        if not valid:
            return "outside validity window"
        decision = doc.get("decision")
        if decision is not None:
            errs = validate_decision(decision)
            if errs:
                return "not AGD-1 conformant: " + "; ".join(errs[:4])
        errs = binding_violations(doc)
        if errs:
            return "counterparty binding violated: " + "; ".join(errs[:4])
        return None

    # -- decisions ------------------------------------------------------------
    def signed_decision(self, capability: str,
                        ttl_seconds: int = 3600) -> tuple[Optional[dict[str, Any]],
                                                          str, Optional[float]]:
        """-> (signed_envelope|None, channel, age_seconds).

        channel: "live" (fetched AND fully verified now), "cache" (served from
        the signed cache — every cache read re-verifies; envelope may be past
        valid_until, age says how old), "unverified" (the Guild answered but
        the document failed verification — treated as no evidence, never as
        live), or "outage" (nothing verifiable available)."""
        q = urllib.parse.urlencode({"capability": capability, "signed": "true",
                                    "ttl_seconds": ttl_seconds})
        fetched: Optional[dict[str, Any]] = None
        failure: Optional[str] = None
        try:
            fetched = self._get(f"/check?{q}")
        except Exception:
            fetched = None
        if fetched is not None:
            failure = self._verify_live(fetched)
            if failure is None:
                self.stats["live_fetches"] += 1
                if self.cache is not None:
                    self.cache.put("decision", capability, fetched)
                return fetched, "live", 0.0
            self.stats["live_verify_failures"] += 1
            self.last_verify_failure = failure
        if self.cache is not None:
            doc, state, age = self.cache.get("decision", capability)
            if doc is not None:
                self.stats["cache_serves"] += 1
                return doc, "cache", age
        self.stats["outages"] += 1
        return None, ("unverified" if failure is not None else "outage"), None

    # -- passports -------------------------------------------------------------
    def passport(self, agent_id: str) -> Optional[dict[str, Any]]:
        try:
            doc = self._get(f"/agents/{urllib.parse.quote(agent_id)}/passport")
            v = verify_data_integrity(doc)
            if not v["verified"] or not self._issuer_allowed(v["issuer_did"] or ""):
                return None
            if self.cache is not None:
                self.cache.put("passport", agent_id, doc)
            return doc
        except Exception:
            if self.cache is not None:
                doc, _state, _age = self.cache.get("passport", agent_id)
                return doc
            return None

    # -- outcome reporting -----------------------------------------------------
    def post_signed_outcome(self, doc: dict[str, Any]) -> Optional[dict[str, Any]]:
        try:
            return self._post("/outcomes", doc)
        except Exception:
            return None

    def ledger_record(self, record_id: str) -> Optional[dict[str, Any]]:
        try:
            return self._get(f"/ledger/record/{urllib.parse.quote(record_id)}"
                             ).get("record")
        except Exception:
            return None

    def record_collaboration(self, body: dict[str, Any]) -> Optional[dict[str, Any]]:
        try:
            return self._post("/collaborations", body)
        except Exception:
            return None
