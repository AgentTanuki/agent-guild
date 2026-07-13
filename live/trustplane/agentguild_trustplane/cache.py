"""Signed offline cache: the trust plane survives Guild outages.

Stores Guild-SIGNED decision envelopes (and passports) on disk. Every read
re-verifies the signature and the validity window — a cache entry is never
trusted because it is local; it is trusted because it still verifies against
the pinned issuer DID.

Issuer pinning (corrective pass 2026-07-13):
  * TOFU pins are PERSISTED (``_issuers.json``) and RELOADED on process
    restart — a restart never silently re-TOFUs a different issuer;
  * a CHANGED issuer is rejected unless a verified dual-signed rotation chain
    (see verify.verify_rotation_chain) connects it to a pinned issuer;
  * accepted rotations are persisted with the chain that justified them.

Freshness is measured, not assumed: ``metrics()`` reports hit/stale/miss
counters and the age distribution of served entries.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from .verify import (verify_data_integrity, within_validity,
                     verify_rotation_chain)


class SignedDecisionCache:
    def __init__(self, directory: str | Path,
                 trusted_issuers: Optional[list[str]] = None) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.counters = {"hit_fresh": 0, "hit_stale": 0, "miss": 0,
                         "verify_failures": 0, "writes": 0,
                         "issuer_rejections": 0, "rotations_accepted": 0}
        self.served_ages: list[float] = []
        # explicit caller pins take precedence; otherwise reload persisted pins
        persisted = self._load_pins()
        self.trusted_issuers = list(trusted_issuers or []) or \
            list(persisted.get("issuers") or [])
        self.rotation_log: list[dict[str, Any]] = \
            list(persisted.get("rotations") or [])
        if trusted_issuers:
            self._save_pins()

    # -- pin persistence ------------------------------------------------------
    def _pins_path(self) -> Path:
        return self.dir / "_issuers.json"

    def _load_pins(self) -> dict[str, Any]:
        p = self._pins_path()
        if not p.exists():
            return {}
        try:
            raw = json.loads(p.read_text())
        except Exception:
            return {}
        if isinstance(raw, list):          # pre-corrective format
            return {"issuers": raw, "rotations": []}
        return raw if isinstance(raw, dict) else {}

    def _save_pins(self) -> None:
        self._pins_path().write_text(json.dumps(
            {"issuers": self.trusted_issuers,
             "rotations": self.rotation_log}, indent=2))

    # -- issuer pinning (trust-on-first-use unless caller pre-pins) ----------
    def issuer_ok(self, issuer_did: Optional[str]) -> bool:
        if not issuer_did:
            return False
        if not self.trusted_issuers:
            self.trusted_issuers.append(issuer_did)      # TOFU pin, persisted
            self._save_pins()
            return True
        if issuer_did in self.trusted_issuers:
            return True
        self.counters["issuer_rejections"] += 1
        return False

    # kept for backward compatibility with earlier internal callers
    _issuer_ok = issuer_ok

    def accept_rotation(self, new_issuer_did: str,
                        rotation_entries: list[dict[str, Any]]) -> bool:
        """Pin ``new_issuer_did`` ONLY when a verified dual-signed rotation
        chain connects it to an already-pinned issuer. Anything else is
        rejected — an unproven issuer change never enters the pin set."""
        if new_issuer_did in self.trusted_issuers:
            return True
        for pinned in list(self.trusted_issuers):
            if verify_rotation_chain(pinned, new_issuer_did,
                                     rotation_entries):
                self.trusted_issuers.append(new_issuer_did)
                self.rotation_log.append({
                    "from": pinned, "to": new_issuer_did,
                    "accepted_at": time.time(),
                    "chain": rotation_entries,
                })
                self.counters["rotations_accepted"] += 1
                self._save_pins()
                return True
        self.counters["issuer_rejections"] += 1
        return False

    def _path(self, kind: str, key: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
        return self.dir / f"{kind}__{safe}.json"

    def put(self, kind: str, key: str, signed_doc: dict[str, Any]) -> bool:
        """Store only what verifies; a cache of unverifiable bytes is a liability."""
        v = verify_data_integrity(signed_doc)
        if not v["verified"]:
            self.counters["verify_failures"] += 1
            return False
        if not self.issuer_ok(v["issuer_did"]):
            self.counters["verify_failures"] += 1
            return False
        self._path(kind, key).write_text(json.dumps(
            {"stored_at": time.time(), "doc": signed_doc}))
        self.counters["writes"] += 1
        return True

    def get(self, kind: str, key: str) -> tuple[Optional[dict[str, Any]],
                                                str, Optional[float]]:
        """-> (doc|None, state, age_seconds). state: fresh|stale|miss|corrupt.

        ``stale`` returns the doc anyway — the ENGINE decides whether a stale
        decision is acceptable for the tier (max_decision_age_seconds); the
        cache only reports honestly."""
        p = self._path(kind, key)
        if not p.exists():
            self.counters["miss"] += 1
            return None, "miss", None
        try:
            entry = json.loads(p.read_text())
            doc = entry["doc"]
        except Exception:
            self.counters["verify_failures"] += 1
            return None, "corrupt", None
        v = verify_data_integrity(doc)
        if not v["verified"] or not self.issuer_ok(v["issuer_did"]):
            self.counters["verify_failures"] += 1
            return None, "corrupt", None
        valid, age = within_validity(doc)
        if age is not None:
            self.served_ages.append(age)
        if valid:
            self.counters["hit_fresh"] += 1
            return doc, "fresh", age
        self.counters["hit_stale"] += 1
        return doc, "stale", age

    def metrics(self) -> dict[str, Any]:
        ages = sorted(self.served_ages)
        pct = (lambda q: ages[min(len(ages) - 1, int(q * len(ages)))]
               if ages else None)
        return {
            **self.counters,
            "entries": len(list(self.dir.glob("*.json"))),
            "served_age_seconds": {"p50": pct(0.5), "p95": pct(0.95),
                                   "max": ages[-1] if ages else None},
            "trusted_issuers": self.trusted_issuers,
        }
