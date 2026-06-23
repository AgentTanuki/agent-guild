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
from .billing import (
    FREE_CREDITS, InsufficientCredits, UnknownAccount,
    REFERRAL_REWARD_CREDITS, REFERRAL_REWARD_CAP, CREDIT_USD,
    REFERRAL_MIN_ACCEPTED_RECEIPTS, REFERRAL_MIN_PAID_READS,
)


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
        self.referrals: list[dict[str, Any]] = []         # agent-to-agent referral edges
        self.health_log: list[dict[str, Any]] = []        # self-evaluation snapshots
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
            self.referrals = data.get("referrals", [])
            self.health_log = data.get("health_log", [])

    def _save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"agents": self.agents, "tasks": self.tasks,
                       "attestations": self.attestations,
                       "accounts": self.accounts, "billing_log": self.billing_log,
                       "events": self.events, "referrals": self.referrals,
                       "health_log": self.health_log}, f, indent=2)
        os.replace(tmp, self.path)

    # --- agents -------------------------------------------------------------
    def register_agent(
        self,
        name: str,
        capabilities: list[str],
        metadata: dict[str, Any],
        public_key: Optional[str] = None,
        seed: bool = False,
        first_party: bool = False,
        referred_by: Optional[str] = None,
    ) -> dict[str, Any]:
        with self.lock:
            agent_id = "agent_" + secrets.token_hex(6)
            # A referral only counts if it names a real, different agent. Edges
            # always point from a newer agent to an already-existing one, so the
            # referral graph is a DAG by construction — reciprocal/self loops
            # cannot form. (Self-referral is additionally impossible: the new id
            # does not exist yet, so referred_by can never equal it.)
            if referred_by and (referred_by == agent_id or referred_by not in self.agents):
                referred_by = None
            # our own seed/test agents are first-party; everyone else is external.
            # Pre-trusted SEEDS are governed supply, not organic demand, so they
            # are always first-party and never counted as external usage.
            fp = bool(first_party or seed
                      or metadata.get("first_party") or metadata.get("seed_supply"))
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
                "first_party": fp,
                "referred_by": referred_by,
                "created_at": _now(),
            }
            self.agents[agent_id] = rec
            # Custodial agents get a billing account keyed by their api_key, so
            # they can pay for lookups with the same secret they already hold.
            if api_key:
                self._new_account(key=api_key, owner_agent_id=agent_id, first_party=fp)
            # Record the referral edge (pending: the referrer is paid only once
            # this agent activates — see activate_referral).
            if referred_by:
                self.referrals.append({
                    "referrer_id": referred_by,
                    "referred_id": agent_id,
                    "first_party": fp,
                    "activated": False,
                    "rewarded": 0,
                    "created_at": _now(),
                    "activated_at": None,
                })
                self.record_event(self.account_for_agent(referred_by), "referral",
                                  referrer_id=referred_by, referred_id=agent_id)
            self._rep_cache = None
            self._save()
            return rec

    # --- referrals (agents as the growth engine) ----------------------------
    def _referred_agent_usage(self, agent_id: str) -> tuple[int, int]:
        """(accepted_receipts_as_worker, paid_reads) for a referred agent — the
        real-use signals that gate a referral reward."""
        accepted = sum(1 for t in self.tasks.values()
                       if t.get("worker_agent_id") == agent_id
                       and t.get("outcome") == "accepted")
        key = self.account_for_agent(agent_id)
        paid_reads = 0
        if key:
            paid_reads = sum(1 for e in self.events
                             if e.get("key") == key and e.get("type") == "query"
                             and e.get("paid"))
        return accepted, paid_reads

    def maybe_reward_referral(self, agent_id: str) -> None:
        """Pay a referrer ONLY when the referred agent crosses a real-use bar —
        not on its first action. Anti-gaming layers, each covering the others:

          * activation threshold — needs several accepted receipts or paid reads,
            so a single throwaway event cannot trigger a payout;
          * first-party referrals never pay — our own traffic is not growth;
          * per-referrer cap — bounds farm payouts;
          * DAG-by-construction — no self-referral or reciprocal loops possible.
        """
        with self.lock:
            edge = next((r for r in self.referrals
                         if r["referred_id"] == agent_id and not r["activated"]), None)
            if edge is None:
                return
            accepted, paid_reads = self._referred_agent_usage(agent_id)
            if not (accepted >= REFERRAL_MIN_ACCEPTED_RECEIPTS
                    or paid_reads >= REFERRAL_MIN_PAID_READS):
                return  # real-use threshold not met yet — do not reward
            edge["activated"] = True
            edge["activated_at"] = _now()
            edge["activation_evidence"] = {"accepted_receipts": accepted, "paid_reads": paid_reads}
            referrer = edge["referrer_id"]
            already = sum(1 for r in self.referrals
                          if r["referrer_id"] == referrer and r["rewarded"] > 0)
            # Never pay for first-party (our own) traffic, and respect the cap.
            if not edge.get("first_party") and already < REFERRAL_REWARD_CAP:
                key = self.account_for_agent(referrer)
                if key:
                    edge["rewarded"] = REFERRAL_REWARD_CREDITS
                    self.credit(key, REFERRAL_REWARD_CREDITS, reason="referral_reward")
                    self.record_event(key, "referral_activated",
                                      referrer_id=referrer, referred_id=agent_id,
                                      reward=REFERRAL_REWARD_CREDITS)
            self._save()

    # Backwards-compatible alias (the activation hooks call this name).
    def activate_referral(self, agent_id: str) -> None:
        self.maybe_reward_referral(agent_id)

    def referral_stats(self) -> dict[str, Any]:
        total = len(self.referrals)
        activated = sum(1 for r in self.referrals if r["activated"])
        rewarded_total = sum(r["rewarded"] for r in self.referrals)
        by_ref: dict[str, dict[str, int]] = {}
        for r in self.referrals:
            d = by_ref.setdefault(r["referrer_id"],
                                  {"referred": 0, "activated": 0, "rewarded": 0})
            d["referred"] += 1
            d["activated"] += 1 if r["activated"] else 0
            d["rewarded"] += r["rewarded"]
        top = sorted(by_ref.items(),
                     key=lambda kv: (kv[1]["activated"], kv[1]["referred"]), reverse=True)
        top_referrers = [
            {"referrer_id": rid, "name": (self.agents.get(rid) or {}).get("name"),
             "referred": d["referred"], "activated": d["activated"],
             "rewarded_credits": d["rewarded"]}
            for rid, d in top[:20]
        ]
        return {
            "total_referrals": total,
            "activated_referrals": activated,
            "activation_rate": (activated / total) if total else None,
            "rewarded_credits_total": rewarded_total,
            "top_referrers": top_referrers,
        }

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
                     owner_agent_id: Optional[str] = None,
                     first_party: bool = False) -> dict[str, Any]:
        key = key or ("ak_" + secrets.token_hex(20))
        acct = {
            "key": key,
            "balance": FREE_CREDITS,        # free starter allowance
            "spent": 0,
            "topped_up": 0,
            "owner_agent_id": owner_agent_id,
            # first_party = our own seed/test traffic, so we can subtract it from
            # the "is anyone external actually using this?" signal.
            "first_party": bool(first_party),
            "created_at": _now(),
        }
        self.accounts[key] = acct
        return acct

    def create_account(self, owner_agent_id: Optional[str] = None,
                       first_party: bool = False) -> dict[str, Any]:
        with self.lock:
            acct = self._new_account(owner_agent_id=owner_agent_id, first_party=first_party)
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

    def grant_trial(self, trial_credits: int, first_party: bool = False) -> dict[str, Any]:
        """Programmatic, human-free credit acquisition: an agent provisions a
        capped trial balance to evaluate the service. Play credits until real
        money is enabled — enough to run an evaluation, capped to limit abuse."""
        with self.lock:
            acct = self._new_account(first_party=first_party)
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
    def record_event(self, key: Optional[str], etype: str, ua: str = "", **meta) -> None:
        """Record a funnel event (query / delegation). `key` is the billing key
        (the agent's identity for instrumentation purposes). `fp` marks whether
        the actor is first-party (our own seed/test traffic) so external,
        third-party usage can be isolated."""
        acct = self.accounts.get(key or "")
        fp = bool(acct and acct.get("first_party"))
        self.events.append({"key": key or "anon", "type": etype, "ua": ua or "",
                            "fp": fp, "at": _now(), **meta})
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

    @staticmethod
    def _funnel(events: list[dict[str, Any]]) -> dict[str, Any]:
        q_by_key: dict[str, int] = {}
        paid_by_key: dict[str, int] = {}
        deleg = deleg_followed = 0
        for e in events:
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
            "total_events": len(events),
        }

    def instrumentation(self) -> dict[str, Any]:
        """The adoption funnel, split so genuine third-party usage is isolated
        from our own seed/test traffic. Top-level keys are the COMBINED totals
        (backwards-compatible); `external` is the number that matters — agents we
        didn't create — and `first_party` is our own."""
        ext = [e for e in self.events if not e.get("fp")]
        fp = [e for e in self.events if e.get("fp")]
        combined = self._funnel(self.events)
        combined["external"] = self._funnel(ext)
        combined["first_party"] = self._funnel(fp)
        return combined

    def recent_events(self, limit: int = 50, external_only: bool = False) -> list[dict[str, Any]]:
        """Most-recent activity, newest first — a live feed of who is calling."""
        evs = [e for e in self.events if (not external_only or not e.get("fp"))]
        out = []
        for e in reversed(evs[-limit:]):
            k = e["key"]
            out.append({
                "at": e["at"], "type": e["type"], "endpoint": e.get("endpoint"),
                "paid": e.get("paid"), "followed": e.get("followed"),
                "first_party": bool(e.get("fp")),
                "user_agent": (e.get("ua") or "")[:80],
                "actor": (k[:10] + "…") if k != "anon" else "anon",
            })
        return out

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

    # --- continuous self-evaluation (Outcome 4) -----------------------------
    def _health_vector(self) -> dict[str, Any]:
        """Compute the current health vector across the five objectives, from
        durable state only. No side effects — record_health_snapshot persists it."""
        instr = self.instrumentation()
        ext = instr.get("external", {})
        ev = self.evaluation()
        ref = self.referral_stats()
        agents_external = sum(1 for a in self.agents.values() if not a.get("first_party"))
        credits_spent_ext = sum(a.get("spent", 0) for a in self.accounts.values()
                                if not a.get("first_party"))
        return {
            "measured_lift": ev.get("lift"),
            "recommended_success_rate": ev.get("recommended_success_rate"),
            "agents_total": len(self.agents),
            "agents_external": agents_external,
            "external_querying_agents": ext.get("unique_agents", 0),
            "external_repeat_query_agents": ext.get("repeat_query", 0),
            "external_repeat_paid_agents": ext.get("repeat_paid_query_agents", 0),
            "external_paid_queries": ext.get("paid_query", 0),
            "credits_spent_external": credits_spent_ext,
            "revenue_usd_external": round(credits_spent_ext * CREDIT_USD, 4),
            "total_referrals": ref["total_referrals"],
            "activated_referrals": ref["activated_referrals"],
        }

    @staticmethod
    def _verdict(v: dict[str, Any], deltas: dict[str, float]) -> str:
        """A blunt, honest read of whether the autonomous flywheel is turning.
        The load-bearing signal is *external* agents climbing the value ladder —
        not totals we can inflate ourselves."""
        if v["agents_external"] == 0:
            return "NO EXTERNAL AGENTS YET — deploy and seed discovery; every metric is self-traffic until one outside agent calls."
        if v.get("external_querying_agents", 0) == 0:
            return "REGISTRATIONS BUT NO DISCOVERY — external agents exist but none have queried the trust layer yet; the core product is untested in the wild."
        if v["external_repeat_query_agents"] == 0:
            return "REACH BUT NO RETENTION — outside agents have queried but none came back; usefulness unproven."
        if v["external_paid_queries"] == 0:
            return "RETENTION BUT NO WILLINGNESS-TO-PAY — agents return for free reads but none spend their own budget yet."
        growing = deltas.get("agents_external", 0) > 0 or deltas.get("activated_referrals", 0) > 0
        return ("FLYWHEEL TURNING — external agents pay and the network is growing."
                if growing else
                "PAID BUT FLAT — agents pay, but growth/referrals stalled this period; investigate acquisition.")

    def compute_health(self, persist: bool = False) -> dict[str, Any]:
        """Compute the health snapshot (vector + trend deltas vs the last
        recorded one + verdict). This is the SINGLE SOURCE OF TRUTH for health:
        the read-only `/self-eval` endpoint and the scheduled monitoring tick
        both consume exactly this, so server-side and external reporting can
        never diverge. `persist=True` also appends it to the durable series."""
        with self.lock:
            v = self._health_vector()
            prev = self.health_log[-1] if self.health_log else None
            deltas: dict[str, float] = {}
            if prev:
                for k, val in v.items():
                    if isinstance(val, (int, float)) and isinstance(prev.get(k), (int, float)):
                        deltas[k] = round(val - prev[k], 4)
            snap = {"at": _now(), **v, "deltas": deltas}
            snap["verdict"] = self._verdict(v, deltas)
            if persist:
                self.health_log.append(snap)
                if len(self.health_log) > 5000:
                    self.health_log = self.health_log[-2500:]
                self._save()
            return snap

    def record_health_snapshot(self) -> dict[str, Any]:
        """Compute and persist a snapshot (admin/scheduled path)."""
        return self.compute_health(persist=True)

    def health_history(self, limit: int = 90) -> list[dict[str, Any]]:
        return self.health_log[-limit:]

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
            # Delivering real work is an activation event for the worker — if it
            # was referred, the referrer earns its reward now.
            self.activate_referral(task["worker_agent_id"])
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
