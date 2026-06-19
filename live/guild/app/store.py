"""In-memory store with JSON-file persistence — v0.2.

Local-first: no database required. Set GUILD_DATA to choose the file. Holds
agents (including custodial private keys + api keys — local trust service),
**task receipts**, and attestations. Reputation is computed on demand from
evidence-weighted attestations: an attestation that references a real task
receipt (with a deliverable hash, a payment, and/or a stake) counts far more
than a bare assertion. No blockchain, no real money — payment and stake are
simulated values that drive the weighting.
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
from .reputation import score, AttRecord, AgentScore, ScoringResult
from .billing import FREE_CREDITS, InsufficientCredits, UnknownAccount


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- evidence weighting -----------------------------------------------------
# An attestation's influence is governed by the evidence behind it. These are
# the only places "a real transaction happened" enters the score.
W_UNBACKED = 0.15   # a signed assertion with no task receipt — barely counts
W_RECEIPT = 0.55    # references a real task receipt (deliverable hash present)
W_PAYMENT_BONUS = 0.30  # the task carried a (simulated) payment
W_STAKE_BONUS = 0.15    # the issuer staked reputation on the claim
W_DISPUTED = 0.5    # multiplier if the receipt's outcome was disputed


class Store:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.environ.get("GUILD_DATA", "")
        self.lock = threading.RLock()
        self.agents: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.attestations: list[dict[str, Any]] = []
        self.accounts: dict[str, dict[str, Any]] = {}     # billing key -> account
        self.billing_log: list[dict[str, Any]] = []       # usage + top-up ledger
        self.events: list[dict[str, Any]] = []            # agent-native instrumentation
        self._rep_cache: Optional[ScoringResult] = None
        self._load()

    # --- persistence --------------------------------------------------------
    def _load(self) -> None:
        if self.path and os.path.exists(self.path):
            with open(self.path, "r") as f:
                data = json.load(f)
            self.agents = data.get("agents", {})
            self.tasks = data.get("tasks", {})
            self.attestations = data.get("attestations", [])
            self.accounts = data.get("accounts", {})
            self.billing_log = data.get("billing_log", [])
            self.events = data.get("events", [])

    def _save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"agents": self.agents, "tasks": self.tasks,
                       "attestations": self.attestations,
                       "accounts": self.accounts, "billing_log": self.billing_log,
                       "events": self.events}, f, indent=2)
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
            # Custodial agents get a billing account keyed by their api_key, so
            # they can pay for lookups with the same secret they already hold.
            if api_key:
                self._new_account(key=api_key, owner_agent_id=agent_id)
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

    # --- billing accounts / credit ledger -----------------------------------
    def _new_account(self, key: Optional[str] = None,
                     owner_agent_id: Optional[str] = None) -> dict[str, Any]:
        key = key or ("ak_" + secrets.token_hex(20))
        acct = {
            "key": key,
            "balance": FREE_CREDITS,        # free starter allowance
            "spent": 0,
            "topped_up": 0,
            "owner_agent_id": owner_agent_id,
            "created_at": _now(),
        }
        self.accounts[key] = acct
        return acct

    def create_account(self, owner_agent_id: Optional[str] = None) -> dict[str, Any]:
        with self.lock:
            acct = self._new_account(owner_agent_id=owner_agent_id)
            self._save()
            return acct

    def get_account(self, key: str) -> Optional[dict[str, Any]]:
        return self.accounts.get(key)

    def charge(self, key: str, cost: int, endpoint: str) -> dict[str, Any]:
        """Draw `cost` credits from an account. Raises UnknownAccount or
        InsufficientCredits. Returns the account."""
        with self.lock:
            acct = self.accounts.get(key)
            if acct is None:
                raise UnknownAccount(key)
            if acct["balance"] < cost:
                raise InsufficientCredits(acct["balance"], cost)
            acct["balance"] -= cost
            acct["spent"] += cost
            self.billing_log.append({
                "key": key, "type": "charge", "endpoint": endpoint,
                "amount": -cost, "balance_after": acct["balance"], "at": _now(),
            })
            self._save()
            return acct

    def credit(self, key: str, credits: int, reason: str = "topup") -> dict[str, Any]:
        with self.lock:
            acct = self.accounts.get(key)
            if acct is None:
                raise UnknownAccount(key)
            acct["balance"] += credits
            acct["topped_up"] += credits
            self.billing_log.append({
                "key": key, "type": reason, "amount": credits,
                "balance_after": acct["balance"], "at": _now(),
            })
            self._save()
            return acct

    def grant_trial(self, trial_credits: int) -> dict[str, Any]:
        """Programmatic, human-free credit acquisition: an agent provisions a
        capped trial balance to evaluate the service. Play credits until real
        money is enabled — enough to run an evaluation, capped to limit abuse."""
        with self.lock:
            acct = self._new_account()
            acct["balance"] += trial_credits
            acct["topped_up"] += trial_credits
            acct["trial"] = True
            self.billing_log.append({
                "key": acct["key"], "type": "trial_grant", "amount": trial_credits,
                "balance_after": acct["balance"], "at": _now(),
            })
            self._save()
            return acct

    # --- agent-native instrumentation ---------------------------------------
    def record_event(self, key: Optional[str], etype: str, **meta) -> None:
        """Record a funnel event (query / delegation). `key` is the billing key
        (the agent's identity for instrumentation purposes)."""
        self.events.append({"key": key or "anon", "type": etype, "at": _now(), **meta})
        # keep the persisted log bounded
        if len(self.events) > 50000:
            self.events = self.events[-25000:]

    def note_recommendations(self, key: Optional[str], worker_ids: list[str]) -> None:
        """Remember what we just recommended to `key`, so a later hire of one of
        those workers can be attributed as 'delegation following a recommendation'."""
        if not key:
            return
        acct = self.accounts.get(key)
        if acct is None:
            return
        recs = acct.setdefault("recent_recs", [])
        for w in worker_ids:
            recs.append(w)
        acct["recent_recs"] = recs[-50:]

    def followed_recommendation(self, key: Optional[str], worker_id: str) -> bool:
        acct = self.accounts.get(key or "")
        return bool(acct and worker_id in acct.get("recent_recs", []))

    def account_for_agent(self, agent_id: str) -> Optional[str]:
        for k, a in self.accounts.items():
            if a.get("owner_agent_id") == agent_id:
                return k
        return None

    def instrumentation(self) -> dict[str, Any]:
        """The funnel: first query, repeat query, paid query, repeat paid query,
        and delegations following a recommendation."""
        q_by_key: dict[str, int] = {}
        paid_by_key: dict[str, int] = {}
        deleg = deleg_followed = 0
        for e in self.events:
            if e["type"] == "query":
                q_by_key[e["key"]] = q_by_key.get(e["key"], 0) + 1
                if e.get("paid"):
                    paid_by_key[e["key"]] = paid_by_key.get(e["key"], 0) + 1
            elif e["type"] == "delegation":
                deleg += 1
                if e.get("followed"):
                    deleg_followed += 1
        return {
            "unique_agents": len(q_by_key),
            "first_query": len([k for k, n in q_by_key.items() if n >= 1]),
            "repeat_query": len([k for k, n in q_by_key.items() if n >= 2]),
            "paid_query": sum(paid_by_key.values()),
            "agents_with_paid_query": len(paid_by_key),
            "repeat_paid_query_agents": len([k for k, n in paid_by_key.items() if n >= 2]),
            "delegations": deleg,
            "delegations_following_recommendation": deleg_followed,
            "total_events": len(self.events),
        }

    def evaluation(self, trust_threshold: float = 50.0) -> dict[str, Any]:
        """Measured outcome lift: success rate of hires of *recommended* (high-
        trust) workers vs everyone else, from graded task receipts. This is the
        signal an agent uses to verify the Guild improves outcomes."""
        scores = self.reputation()
        rec_succ = rec_tot = base_succ = base_tot = 0
        for t in self.tasks.values():
            outcome = t.get("outcome")
            if outcome not in ("accepted", "disputed", "rejected"):
                continue  # only graded tasks count
            s = scores.get(t["worker_agent_id"])
            trust = s.trust if s else 0.0
            success = 1 if outcome == "accepted" else 0
            if trust >= trust_threshold:
                rec_tot += 1; rec_succ += success
            else:
                base_tot += 1; base_succ += success
        rec_rate = (rec_succ / rec_tot) if rec_tot else None
        base_rate = (base_succ / base_tot) if base_tot else None
        lift = (rec_rate - base_rate) if (rec_rate is not None and base_rate is not None) else None
        return {
            "recommended_success_rate": rec_rate, "n_recommended": rec_tot,
            "baseline_success_rate": base_rate, "n_baseline": base_tot,
            "lift": lift, "trust_threshold": trust_threshold,
        }

    # --- tasks / receipts ---------------------------------------------------
    def create_task(
        self,
        requester_id: str,
        worker_id: str,
        task_type: str,
        payment: float = 0.0,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        with self.lock:
            task_id = "task_" + secrets.token_hex(6)
            rec = {
                "id": task_id,
                "requester_agent_id": requester_id,
                "worker_agent_id": worker_id,
                "task_type": task_type,
                "payment": float(payment),     # simulated cost/payment
                "metadata": metadata or {},
                "deliverable_hash": None,
                "deliverable_url": None,
                "outcome": "open",             # open -> delivered -> accepted/disputed
                "created_at": _now(),
                "delivered_at": None,
            }
            self.tasks[task_id] = rec
            self._save()
            return rec

    def submit_receipt(
        self,
        task_id: str,
        deliverable_hash: str,
        deliverable_url: Optional[str] = None,
        outcome: str = "delivered",
    ) -> dict[str, Any]:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                raise ValueError("task not found")
            task["deliverable_hash"] = deliverable_hash
            task["deliverable_url"] = deliverable_url
            task["outcome"] = outcome
            task["delivered_at"] = _now()
            self._rep_cache = None
            self._save()
            return task

    def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        return self.tasks.get(task_id)

    def tasks_for(self, worker_id: str) -> list[dict[str, Any]]:
        return [t for t in self.tasks.values() if t["worker_agent_id"] == worker_id]

    def receipt_counts(self) -> dict[str, int]:
        """Per-agent count of delivered, non-rejected task receipts (as worker)."""
        counts: dict[str, int] = {}
        for t in self.tasks.values():
            if t.get("deliverable_hash") and t.get("outcome") != "rejected":
                w = t["worker_agent_id"]
                counts[w] = counts.get(w, 0) + 1
        return counts

    # --- evidence weighting -------------------------------------------------
    def _evidence_weight(self, att: dict[str, Any]) -> float:
        """How much this attestation should count, in [0,1]."""
        task = self.tasks.get(att.get("task_id") or "")
        stake = float(att.get("stake", 0.0) or 0.0)
        backed = (
            task is not None
            and task.get("deliverable_hash")
            # the receipt must actually be for THIS issuer hiring THIS subject
            and task.get("requester_agent_id") == att["issuer_id"]
            and task.get("worker_agent_id") == att["subject_id"]
        )
        if not backed:
            # An unbacked assertion barely counts — and a stake on thin air buys
            # nothing, since stake is only evidence when there is a real task it
            # can be slashed against.
            return W_UNBACKED
        w = W_RECEIPT
        if float(task.get("payment", 0.0) or 0.0) > 0:
            w += W_PAYMENT_BONUS
        if task.get("outcome") == "disputed":
            w *= W_DISPUTED
        if stake > 0:
            w = min(1.0, w + W_STAKE_BONUS)
        return min(1.0, max(0.0, w))

    # --- attestations -------------------------------------------------------
    def add_custodial_attestation(
        self,
        issuer: dict[str, Any],
        subject: dict[str, Any],
        capability: str,
        rating: float,
        task_id: str,
        comment: str,
        stake: float = 0.0,
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
            return self._store_attestation(
                att_id, issuer["id"], subject["id"], capability, rating, cred,
                task_id=task_id, stake=stake,
            )

    def add_signed_attestation(
        self, credential: dict[str, Any], stake: float = 0.0
    ) -> dict[str, Any]:
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
                task_id=cs.get("taskId", "n/a"), stake=stake,
            )

    def _store_attestation(
        self, att_id, issuer_id, subject_id, capability, rating, cred,
        task_id="n/a", stake=0.0,
    ) -> dict[str, Any]:
        verified = verify_credential(cred)
        rec = {
            "id": att_id,
            "issuer_id": issuer_id,
            "subject_id": subject_id,
            "capability": capability,
            "rating": float(rating),
            "task_id": task_id,
            "stake": float(stake or 0.0),
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
    def _result(self) -> ScoringResult:
        with self.lock:
            if self._rep_cache is None:
                ids = list(self.agents.keys())
                records = [
                    AttRecord(
                        reviewer=a["issuer_id"],
                        subject=a["subject_id"],
                        rating=a["rating"],
                        weight=self._evidence_weight(a),
                        stake=float(a.get("stake", 0.0) or 0.0),
                    )
                    for a in self.attestations
                    if a["verified"]
                ]
                self._rep_cache = score(
                    ids, records, self.seeds(), self.receipt_counts()
                )
            return self._rep_cache

    def reputation(self) -> dict[str, AgentScore]:
        return self._result().scores

    def flags(self) -> dict[str, Any]:
        return self._result().flags

    def evidence(self, agent_id: str) -> dict[str, Any]:
        """The evidence behind an agent's score: which attestations, which
        receipts, and how each was weighted."""
        s = self._result().scores.get(agent_id)
        atts = []
        for a in self.attestations_for(agent_id):
            atts.append({
                "id": a["id"],
                "issuer_id": a["issuer_id"],
                "rating": a["rating"],
                "task_id": a.get("task_id"),
                "stake": a.get("stake", 0.0),
                "verified": a["verified"],
                "evidence_weight": round(self._evidence_weight(a), 3),
            })
        receipts = [
            {"id": t["id"], "requester": t["requester_agent_id"],
             "task_type": t["task_type"], "payment": t["payment"],
             "deliverable_hash": t["deliverable_hash"], "outcome": t["outcome"]}
            for t in self.tasks_for(agent_id) if t.get("deliverable_hash")
        ]
        return {"score": s, "attestations": atts, "receipts": receipts}
