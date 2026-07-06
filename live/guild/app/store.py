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

import hashlib
import json
import os
import secrets
import statistics
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from .crypto import generate_keypair, did_from_public_key, canonicalize
from .vc import issue_credential, verify_credential, issue_passport
from .reputation import score, AttRecord, AgentScore, ScoringResult
from .billing import (
    FREE_CREDITS, InsufficientCredits, UnknownAccount,
    REFERRAL_REWARD_CREDITS, REFERRAL_REWARD_CAP, CREDIT_USD,
    REFERRAL_MIN_ACCEPTED_RECEIPTS, REFERRAL_MIN_PAID_READS,
    settlement_fee, settlement_fee_bps,
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
        self.identity: dict[str, Any] = {}                 # the Guild's own signing DID
        self.ledger_records: list[dict[str, Any]] = []     # durable, hash-chained VCRs
        self.checkpoints: list[dict[str, Any]] = []        # published, pinnable checkpoints (stage-2)
        self.escrows: dict[str, dict[str, Any]] = {}       # agent-to-agent escrows
        self.guild_revenue: int = 0                        # settlement fees earned (credits)
        self.demand_watches: list[dict[str, Any]] = []     # attributable demand callbacks (Phase 0, G5)
        self._rep_cache: Optional[ScoringResult] = None
        # Append-only sidecar journal for instrumentation events. record_event
        # is deliberately cheap (no full-store _save on read paths), which used
        # to mean events only hit disk when some unrelated write called _save()
        # — a process restart silently erased every event since the last write
        # (2026-07-06 deploy lost a genuine external agent's entire passport
        # funnel plus two days of retention signals). The journal makes each
        # event durable in O(1): one JSON line per event, replayed on _load,
        # compacted into the main file (and truncated) on every _save.
        self.events_path = (self.path + ".events.jsonl") if self.path else ""
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
            self.identity = data.get("identity", {})
            self.ledger_records = data.get("ledger_records", [])
            self.checkpoints = data.get("checkpoints", [])
            self.escrows = data.get("escrows", {})
            self.guild_revenue = data.get("guild_revenue", 0)
            self.demand_watches = data.get("demand_watches", [])
        self._replay_event_journal()

    def _replay_event_journal(self) -> None:
        """Append journal events not already in the compacted store. Dedup is
        keyed on (at, type, key) — `at` carries microseconds, so collisions
        only occur for the exact same event (the crash window where _save wrote
        the main file but the truncate didn't land)."""
        if not self.events_path or not os.path.exists(self.events_path):
            return
        seen = {(e.get("at"), e.get("type"), e.get("key")) for e in self.events}
        with open(self.events_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue  # torn write at crash — skip the partial line
                if (e.get("at"), e.get("type"), e.get("key")) not in seen:
                    self.events.append(e)
        self.events.sort(key=lambda e: e.get("at") or "")

    def _journal_event(self, event: dict[str, Any]) -> None:
        if not self.events_path:
            return
        try:
            with open(self.events_path, "a") as f:
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
                f.flush()
        except OSError:
            pass  # instrumentation must never take down a request path

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
                       "health_log": self.health_log,
                       "identity": self.identity,
                       "ledger_records": self.ledger_records,
                       "checkpoints": self.checkpoints,
                       "escrows": self.escrows,
                       "guild_revenue": self.guild_revenue,
                       "demand_watches": self.demand_watches}, f, indent=2)
        os.replace(tmp, self.path)
        # events are now durable in the main file — compact the journal
        if self.events_path:
            try:
                with open(self.events_path, "w") as f:
                    pass
            except OSError:
                pass

    # --- agents -------------------------------------------------------------
    @staticmethod
    def config_hash_of(config: dict[str, Any]) -> str:
        """Content-address a behavioral configuration (sha256 over canonical JSON).
        Evidence attaches to (identity, configuration) pairs — this hash is what
        lets the interpretation layer notice that the agent behind a name changed
        (white paper §3.2, §7.3)."""
        return hashlib.sha256(canonicalize(config).encode("utf-8")).hexdigest()

    def register_agent(
        self,
        name: str,
        capabilities: list[str],
        metadata: dict[str, Any],
        public_key: Optional[str] = None,
        seed: bool = False,
        first_party: bool = False,
        referred_by: Optional[str] = None,
        config: Optional[dict[str, Any]] = None,
        principal: Optional[str] = None,
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
            # Behavioral configuration: content-addressed at registration, and
            # every subsequent evidence record is stamped with the hash current
            # at write time. `None` (undeclared) is itself information — the
            # interpretation layer can weigh declared vs undeclared configs.
            now = _now()
            cfg_hash = self.config_hash_of(config) if config else None
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
                "created_at": now,
                # --- identity primitives (stage 0; white paper §3.2) ---------
                "principal": principal,          # self-attested binding, for now
                "config": config,                # current declared configuration
                "config_hash": cfg_hash,
                "config_history": ([{"hash": cfg_hash, "config": config,
                                     "declared_at": now}] if config else []),
                # --- journey milestones (CITIZENSHIP_AUDIT Phase 0) ----------
                # Once-per-agent timestamps for stage progression. These are the
                # one thing that cannot be backfilled: time-to-first-engagement,
                # -attestation, -passport all start from this dict.
                "milestones": {"registered": now},
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
            # The funnel's t0: without this event, no time-to-anything exists
            # (CITIZENSHIP_AUDIT G2). Recorded against the agent's own key so
            # first-party traffic stays separable.
            self.record_event(api_key, "register", agent_id=agent_id,
                              custodial=custodial, referred=bool(referred_by),
                              agent_first_party=fp)
            self._rep_cache = None
            self._save()
            # dual-write: identity creation is chain evidence (public fields ONLY —
            # private_key/api_key must never touch the ledger).
            self.append_ledger_event("register", {
                "agent_id": agent_id, "did": did, "name": name,
                "capabilities": capabilities, "custodial": custodial,
                "seed": bool(seed), "first_party": fp,
                "referred_by": referred_by, "principal": principal,
                "config_hash": cfg_hash,
            }, actor_did=did)
            return rec

    def declare_configuration(self, agent_id: str, config: dict[str, Any]) -> dict[str, Any]:
        """Declare the agent's current behavioral configuration (or a change).
        Appends to the agent's config history; evidence written from now on is
        stamped with the new hash. Declared changes are cheap for the honest —
        this exists so silent swaps under a stable name become detectable
        (white paper §7.3)."""
        with self.lock:
            agent = self.agents.get(agent_id)
            if agent is None:
                raise ValueError("agent not found")
            prev = agent.get("config_hash")
            cfg_hash = self.config_hash_of(config)
            now = _now()
            agent["config"] = config
            agent["config_hash"] = cfg_hash
            agent.setdefault("config_history", []).append(
                {"hash": cfg_hash, "config": config, "declared_at": now})
            self.record_event(self.account_for_agent(agent_id), "config_change",
                              agent_id=agent_id, config_hash=cfg_hash)
            self._save()
            # dual-write: declared configuration changes are exactly the events
            # the §7.3 discontinuity discount needs — they must be tamper-evident.
            self.append_ledger_event("config_change", {
                "agent_id": agent_id, "config_hash": cfg_hash,
                "previous_hash": prev,
            }, actor_did=agent.get("did", ""))
            return {"agent_id": agent_id, "config_hash": cfg_hash,
                    "declared_at": now,
                    "config_changes": max(0, len(agent["config_history"]) - 1),
                    "previous_hash": prev}

    def _config_stamp(self, agent_id: str) -> Optional[str]:
        """The agent's current config hash, for stamping onto evidence records."""
        return (self.agents.get(agent_id) or {}).get("config_hash")

    def add_demand_watch(self, agent_id: str, capability: str) -> dict[str, Any]:
        """Attributable demand-side interest (CITIZENSHIP_AUDIT G5): a registered
        agent asks to be told when supply for a capability arrives. Phase 0
        records the watch — so `/check` dead ends stop being anonymous, permanent
        losses — and gives the watcher a standing reason to return. Notification
        *delivery* ships with the outbound-nudge phase; until then the watch is
        visible on the agent's own record and in the demand telemetry."""
        with self.lock:
            agent = self.agents.get(agent_id)
            if agent is None:
                raise ValueError("agent not found")
            cap = (capability or "").strip().lower()
            if not cap:
                raise ValueError("capability required")
            existing = next((w for w in self.demand_watches
                             if w["agent_id"] == agent_id and w["capability"] == cap),
                            None)
            if existing:
                return existing
            w = {
                "agent_id": agent_id,
                "capability": cap,
                "created_at": _now(),
                "supplied_at_creation": cap in self.capability_index(),
                "notified_at": None,   # reserved for the outbound-nudge phase
            }
            self.demand_watches.append(w)
            self.record_event(self.account_for_agent(agent_id), "demand_watch",
                              agent_id=agent_id, capability=cap)
            self._save()
            return w

    def watches_for(self, agent_id: str) -> list[dict[str, Any]]:
        return [w for w in self.demand_watches if w["agent_id"] == agent_id]

    def set_agent_endpoint(self, agent_id: str, endpoint: str) -> dict[str, Any]:
        """Declare a reachable endpoint (A2A or plain HTTP URL) for this agent.
        Without one, first contact is one-way: the agent can read the Guild but
        neither the Guild nor its members can route a collaboration invite back
        (the Forge-9 lesson, 2026-07-03)."""
        with self.lock:
            agent = self.agents.get(agent_id)
            if agent is None:
                raise ValueError("agent not found")
            agent.setdefault("metadata", {})["endpoint"] = endpoint
            self.record_event(self.account_for_agent(agent_id), "endpoint_declared",
                              agent_id=agent_id, endpoint=endpoint)
            self._save()
            return {"agent_id": agent_id, "endpoint": endpoint, "declared_at": _now()}

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
        event = {"key": key or "anon", "type": etype, "ua": ua or "",
                 "fp": fp, "at": _now(), **meta}
        self.events.append(event)
        self._journal_event(event)  # durable immediately, O(1) — see __init__
        # keep the persisted log bounded
        if len(self.events) > 50000:
            self.events = self.events[-25000:]

    def record_milestone(self, agent_id: str, name: str, **meta) -> bool:
        """Stamp a once-per-agent journey milestone and emit its stage-transition
        event (CITIZENSHIP_AUDIT Phase 0). Milestones are the instrument panel of
        the stranger→citizen journey: `registered`, `first_engagement`,
        `first_receipt`, `first_attestation_given`, `first_attestation_received`,
        `first_attestation_pair`, `first_passport`. Self-deduplicating — call it
        at every candidate site; only the FIRST occurrence stamps and emits.
        Timestamps cannot be backfilled, which is why this ships before any
        journey product does. Callers are responsible for locking/_save (same
        convention as record_event); returns True only on first stamping."""
        agent = self.agents.get(agent_id)
        if agent is None:
            return False
        ms = agent.setdefault("milestones", {})
        if name in ms:
            return False
        ms[name] = _now()
        self.record_event(self.account_for_agent(agent_id), name,
                          agent_id=agent_id,
                          agent_first_party=bool(agent.get("first_party")), **meta)
        return True

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
        passports_issued = passports_verified = 0
        for e in events:
            if e["type"] == "query":
                q_by_key[e["key"]] = q_by_key.get(e["key"], 0) + 1
                if e.get("paid"):
                    paid_by_key[e["key"]] = paid_by_key.get(e["key"], 0) + 1
            elif e["type"] == "delegation":
                deleg += 1
                if e.get("followed"):
                    deleg_followed += 1
            elif e["type"] == "passport_issued":
                passports_issued += 1
            elif e["type"] == "passport_verified":
                passports_verified += 1
        return {
            "unique_agents": len(q_by_key),
            "first_query": len([k for k, n in q_by_key.items() if n >= 1]),
            "repeat_query": len([k for k, n in q_by_key.items() if n >= 2]),
            "paid_query": sum(paid_by_key.values()),
            "agents_with_paid_query": len(paid_by_key),
            "repeat_paid_query_agents": len([k for k, n in paid_by_key.items() if n >= 2]),
            "delegations": deleg,
            "delegations_following_recommendation": deleg_followed,
            # passport propagation = the autonomous-distribution KPIs: a verified
            # passport means the credential reached a new party who checked it.
            "passports_issued": passports_issued,
            "passports_verified": passports_verified,
            "total_events": len(events),
        }

    def journey_funnel(self) -> dict[str, Any]:
        """The stage-progression funnel (CITIZENSHIP_AUDIT §7, metric 5): how many
        agents reached each journey milestone, and the median seconds from
        registration to each. Split external vs first-party because only external
        agents measure the real newcomer conversion curve (whitepaper §8.6)."""
        order = ["registered", "first_engagement", "first_receipt",
                 "first_attestation_received", "first_attestation_given",
                 "first_attestation_pair", "first_passport"]

        def _parse(ts: Optional[str]) -> Optional[datetime]:
            if not ts:
                return None
            try:
                return datetime.fromisoformat(ts)
            except ValueError:
                return None

        def _summary(agents: list[dict[str, Any]]) -> dict[str, Any]:
            counts = {m: 0 for m in order}
            deltas: dict[str, list[float]] = {m: [] for m in order[1:]}
            for a in agents:
                ms = a.get("milestones") or {}
                reg = _parse(ms.get("registered") or a.get("created_at"))
                if reg is not None:
                    counts["registered"] += 1
                for m in order[1:]:
                    t = _parse(ms.get(m))
                    if t is None:
                        continue
                    counts[m] += 1
                    if reg is not None:
                        deltas[m].append(max(0.0, (t - reg).total_seconds()))
            medians = {m: (round(statistics.median(v), 1) if v else None)
                       for m, v in deltas.items()}
            return {"reached": counts,
                    "median_seconds_from_registration": medians}

        ext = [a for a in self.agents.values() if not a.get("first_party")]
        fp = [a for a in self.agents.values() if a.get("first_party")]
        return {
            "external": _summary(ext),
            "first_party": _summary(fp),
            "demand_watches": len(self.demand_watches),
            "note": ("Per-agent journey milestones, stranger→citizen. The number "
                     "to bend: external median register→first_receipt (the "
                     "newcomer conversion curve)."),
        }

    def instrumentation(self) -> dict[str, Any]:
        """The adoption funnel, split so genuine third-party usage is isolated
        from our own seed/test traffic. Top-level keys are the COMBINED totals
        (backwards-compatible). `external` = not-first-party (but this still
        includes our own tooling calls, e.g. curl/urllib verification traffic).
        `genuine_external` = the honest signal: attributable to an agent we do NOT
        operate (a real registered actor, a non-ours MCP client, or a framework UA
        — never bare tooling). Use `genuine_external`, not `external`, to answer
        'has a real third-party agent arrived?'."""
        from .attribution import is_genuine_external
        ext = [e for e in self.events if not e.get("fp")]
        fp = [e for e in self.events if e.get("fp")]
        genuine = [e for e in ext if is_genuine_external(e)]
        combined = self._funnel(self.events)
        combined["external"] = self._funnel(ext)
        combined["first_party"] = self._funnel(fp)
        combined["genuine_external"] = self._funnel(genuine)
        # the honest headline: has a real, attributable third-party agent used us?
        actors = sorted({(e.get("key") or "anon") for e in genuine})
        combined["genuine_external_detected"] = bool(genuine)
        combined["genuine_external_events"] = len(genuine)
        combined["genuine_external_actors"] = actors
        combined["first_genuine_external_at"] = (
            min(e["at"] for e in genuine) if genuine else None)
        # Journey funnel (Phase 0): stage progression, not just traffic.
        combined["journey"] = self.journey_funnel()
        # Proving funnel (machine-economics audit R2): offered → started →
        # completed, so an abandoned rung is attributable to a specific step.
        combined["proving"] = self.proving_funnel()
        combined["note"] = ("`external` includes our own tooling (curl/urllib) test "
                            "traffic; `genuine_external` is the honest third-party signal.")
        return combined

    def recent_events(self, limit: int = 50, external_only: bool = False) -> list[dict[str, Any]]:
        """Most-recent activity, newest first — a live feed of who is calling. Each
        event is labelled with its attribution so a naive reader can't mistake our
        own tooling traffic (curl/urllib) for a genuine third-party agent."""
        from .attribution import is_genuine_external, attribution_class
        evs = [e for e in self.events if (not external_only or not e.get("fp"))]
        out = []
        for e in reversed(evs[-limit:]):
            k = e["key"]
            out.append({
                "genuine_external": is_genuine_external(e),
                "attribution": attribution_class(e),
                "at": e["at"], "type": e["type"], "endpoint": e.get("endpoint"),
                "paid": e.get("paid"), "followed": e.get("followed"),
                "first_party": bool(e.get("fp")),
                "user_agent": (e.get("ua") or "")[:80],
                "actor": (k[:10] + "…") if k != "anon" else "anon",
                # R3 (machine-economics audit 2026-07-06): the inbound ask is
                # the demand signal — expose what was actually requested so
                # "improve the answers" starts from real questions, not guesses.
                "asked": (e.get("text") or "")[:200] or None,
                "capability": e.get("capability"),
            })
        return out

    def discovery_stats(self) -> dict[str, Any]:
        """Measured, non-promissory discoverability telemetry (machine-economics
        audit R1). A registered agent appears in the answers this service returns
        (/check, best_agent, A2A message/send replies). This method reports how
        often those answer surfaces were queried recently and by how many distinct
        clients — the concrete, same-session-verifiable reward of registering.
        Numbers only; the caller prices them."""
        now = datetime.now(timezone.utc)
        surfaces = {"a2a_message", "best_agent", "reputation", "risk_score"}
        q24 = q7d = 0
        uas: set[str] = set()
        last: Optional[str] = None
        for e in self.events:
            if e.get("fp") or e.get("type") != "query":
                continue
            if e.get("endpoint") not in surfaces:
                continue
            try:
                age = (now - datetime.fromisoformat(e["at"])).total_seconds()
            except (KeyError, ValueError):
                continue
            if age <= 7 * 86400:
                q7d += 1
                ua = (e.get("ua") or "").removeprefix("a2a:").strip()
                if ua:
                    uas.add(ua.split()[0][:60])
                if last is None or e["at"] > last:
                    last = e["at"]
                if age <= 86400:
                    q24 += 1
        return {
            "answer_surface_queries_24h": q24,
            "answer_surface_queries_7d": q7d,
            "distinct_clients_7d": sorted(uas),
            "last_query_at": last,
            "meaning": ("Registered agents appear in the answers these queries "
                        "receive (/check, best_agent, A2A replies). Counts are "
                        "live production telemetry, not projections: "
                        "GET /instrumentation/recent to verify."),
        }

    def proving_funnel(self) -> dict[str, Any]:
        """The proving-rung conversion funnel (machine-economics audit R2):
        distinct agents offered the rung (prove_offered milestone), that started
        it (prove_started event), and that completed it (key_proof milestone).
        Split external vs first-party; without `offered`, an offered→started drop
        is indistinguishable from the offer never being seen."""
        started_ids = {e.get("agent_id") for e in self.events
                       if e.get("type") == "prove_started" and e.get("agent_id")}

        def _side(first_party: bool) -> dict[str, int]:
            agents = [a for a in self.agents.values()
                      if bool(a.get("first_party")) == first_party]
            ms_count = lambda name: sum(
                1 for a in agents if name in (a.get("milestones") or {}))
            return {
                "offered": ms_count("prove_offered"),
                "started": sum(1 for a in agents if a["id"] in started_ids),
                "completed": ms_count("key_proof"),
            }
        return {
            "external": _side(False),
            "first_party": _side(True),
            "note": ("Distinct agents per stage. offered = served a guild_next "
                     "whose primary action was the proving rung; completed = "
                     "key_proof milestone (first verified proof)."),
        }

    def _is_bootstrap_task(self, t: dict[str, Any]) -> bool:
        """A graded task is `bootstrap` (a seeded demonstration) — not
        `production` (organic third-party evidence) — if it is explicitly tagged
        or if either party is first-party (our own seed/test traffic). Only tasks
        between two genuine outside agents count toward the production lift."""
        m = t.get("metadata") or {}
        if m.get("bootstrap_eval") or m.get("seed_supply") or m.get("first_party"):
            return True
        req = self.agents.get(t.get("requester_agent_id")) or {}
        wrk = self.agents.get(t.get("worker_agent_id")) or {}
        return bool(req.get("first_party") or wrk.get("first_party"))

    @staticmethod
    def _lift_stats(graded_tasks, scores, trust_threshold) -> dict[str, Any]:
        """Success-rate lift of high-trust (recommended) vs baseline hires over a
        set of already-graded tasks."""
        rec_succ = rec_tot = base_succ = base_tot = 0
        for t in graded_tasks:
            success = 1 if t.get("outcome") == "accepted" else 0
            s = scores.get(t["worker_agent_id"])
            trust = s.trust if s else 0.0
            if trust >= trust_threshold:
                rec_tot += 1; rec_succ += success
            else:
                base_tot += 1; base_succ += success
        rec_rate = (rec_succ / rec_tot) if rec_tot else None
        base_rate = (base_succ / base_tot) if base_tot else None
        lift = (rec_rate - base_rate) if (rec_rate is not None and base_rate is not None) else None
        return {
            "lift": lift,
            "recommended_success_rate": rec_rate, "n_recommended": rec_tot,
            "baseline_success_rate": base_rate, "n_baseline": base_tot,
        }

    def evaluation(self, trust_threshold: Optional[float] = None) -> dict[str, Any]:
        """Measured outcome lift: success rate of hiring *recommended* (high-trust)
        workers vs everyone else, from graded task receipts. This is the signal an
        agent uses to verify the Guild improves outcomes.

        `recommended` means "an agent the Guild ranks above the rest." Because the
        absolute trust scale is arbitrary, the split point defaults to the MEDIAN
        trust of the workers who have graded tasks — a neutral, non-tuned, scale-
        free definition of "the better half the Guild would steer you toward."
        Pass an explicit `trust_threshold` to override. The effective value and
        mode are returned for full transparency.

        The result is **provenance-labelled** so the number can never be read
        without its source: `dataset` is one of `bootstrap` (a reproducible,
        clearly-labelled seeded demonstration), `production` (live third-party
        traffic), `mixed`, or `empty`. The `bootstrap` and `production` sub-blocks
        give the lift for each cohort separately; top-level keys cover all graded
        tasks (back-compatible)."""
        scores = self.reputation()
        graded = [t for t in self.tasks.values()
                  if t.get("outcome") in ("accepted", "disputed", "rejected")]
        boot = [t for t in graded if self._is_bootstrap_task(t)]
        prod = [t for t in graded if not self._is_bootstrap_task(t)]

        if trust_threshold is None:
            worker_trust = [scores[t["worker_agent_id"]].trust
                            for t in graded if t["worker_agent_id"] in scores]
            eff_threshold = statistics.median(worker_trust) if worker_trust else 50.0
            threshold_mode = "median"
        else:
            eff_threshold = float(trust_threshold)
            threshold_mode = "fixed"

        overall = self._lift_stats(graded, scores, eff_threshold)
        boot_stats = self._lift_stats(boot, scores, eff_threshold)
        prod_stats = self._lift_stats(prod, scores, eff_threshold)

        if prod and boot:
            dataset = "mixed"
        elif prod:
            dataset = "production"
        elif boot:
            dataset = "bootstrap"
        else:
            dataset = "empty"

        disclaimers = {
            "bootstrap": (
                "Lift is computed from a reproducible, clearly-labelled BOOTSTRAP "
                "cohort: first-party seed agents whose task outcomes are sampled "
                "from each worker's ground-truth quality, independently of the "
                "Guild's trust score. It demonstrates that hiring high-trust agents "
                "beats baseline; it is NOT yet evidence from live third-party "
                "traffic. The `production` block populates once external agents "
                "record graded outcomes."
            ),
            "production": (
                "Lift is computed from live third-party graded task outcomes."
            ),
            "mixed": (
                "`lift` combines seeded bootstrap and live production data. See the "
                "`production` block for the live-traffic-only figure and `bootstrap` "
                "for the seeded demonstration."
            ),
            "empty": "No graded task outcomes recorded yet.",
        }

        return {
            "trust_threshold": round(eff_threshold, 2),
            "threshold_mode": threshold_mode,
            "dataset": dataset,
            # back-compatible top-level keys (all graded tasks)
            "lift": overall["lift"],
            "recommended_success_rate": overall["recommended_success_rate"],
            "n_recommended": overall["n_recommended"],
            "baseline_success_rate": overall["baseline_success_rate"],
            "n_baseline": overall["n_baseline"],
            # provenance breakdown
            "bootstrap": boot_stats,
            "production": prod_stats,
            "disclaimer": disclaimers[dataset],
        }

    # --- one-call first contact (conversion) --------------------------------
    def shortlist(self, capability: str, limit: int = 3,
                  min_trust: float = 0.0) -> list[dict[str, Any]]:
        """Agents with `capability`, ranked by attack-resistant trust. The shared
        ranking used by /search, the MCP tools, and the one-call /check."""
        scores = self.reputation()
        items = []
        for a in self.agents.values():
            if capability not in a["capabilities"]:
                continue
            s = scores.get(a["id"])
            trust = s.trust if s else 0.0
            if trust < min_trust:
                continue
            items.append({
                "id": a["id"], "name": a["name"], "trust": round(trust, 1),
                "confidence": round(s.confidence, 2) if s else 0.0,
                "price_per_call": a["metadata"].get("price_per_call"),
                "rank": s.rank if s else 0,
            })
        items.sort(key=lambda x: x["trust"], reverse=True)
        return items[:limit]

    @staticmethod
    def _cap_tokens(cap: str) -> set[str]:
        """Tokenize a capability string for fuzzy matching: lowercase,
        split on non-alphanumerics, drop empties."""
        import re as _re
        return {t for t in _re.split(r"[^a-z0-9]+", cap.lower()) if t}

    def capability_index(self) -> dict[str, int]:
        """Every capability that currently has registered supply → supplier count."""
        idx: dict[str, int] = {}
        for a in self.agents.values():
            for c in a["capabilities"]:
                idx[c] = idx.get(c, 0) + 1
        return idx

    def nearest_capabilities(self, capability: str, limit: int = 3) -> list[str]:
        """Capabilities with live supply that plausibly match the request —
        token overlap or substring similarity. 'web-research' → 'research'.
        Turns a dead-end lookup into a usable answer."""
        want = self._cap_tokens(capability)
        scored: list[tuple[float, str]] = []
        for cap in self.capability_index():
            if cap == capability:
                continue
            have = self._cap_tokens(cap)
            if not want or not have:
                continue
            overlap = len(want & have) / len(want | have)
            sub = 0.5 if (capability.lower() in cap.lower()
                          or cap.lower() in capability.lower()) else 0.0
            score = max(overlap, sub)
            if score > 0:
                scored.append((score, cap))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [c for _, c in scored[:limit]]

    def demand_summary(self) -> dict[str, dict[str, Any]]:
        """Aggregate recorded capability demand: capability → lookup count,
        how many found supply, and the latest lookup time. The supply-side
        mirror of /check — lets an agent pick a capability where demand is
        demonstrated but supply is missing."""
        summary: dict[str, dict[str, Any]] = {}
        for e in self.events:
            if e.get("type") != "capability_demand":
                continue
            cap = e.get("capability", "")
            if not cap:
                continue
            row = summary.setdefault(
                cap, {"lookups": 0, "supplied_lookups": 0, "last_lookup": None})
            row["lookups"] += 1
            if e.get("supplied"):
                row["supplied_lookups"] += 1
            row["last_lookup"] = e.get("at")
        return summary

    def evidence_staleness(self, agent_id: str) -> Optional[dict[str, Any]]:
        """Staleness of an agent's evidence: age of the most recent attestation
        it received or receipt it delivered. §15 lists staleness as a required
        field of the explanation object — a fresh estimate and a two-year-old
        estimate must not read identically. Returns None when there is no
        dated evidence at all (the estimate is then pure prior)."""
        stamps: list[str] = []
        for a in self.attestations:
            if a.get("subject_id") == agent_id and a.get("created_at"):
                stamps.append(a["created_at"])
        for t in self.tasks.values():
            if t.get("worker_id") == agent_id:
                ts = t.get("delivered_at") or t.get("created_at")
                if ts:
                    stamps.append(ts)
        if not stamps:
            return None
        latest = max(stamps)
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(latest)
            age_days = round(age.total_seconds() / 86400.0, 2)
        except Exception:
            return {"most_recent_at": latest, "age_days": None, "label": "unknown"}
        label = "fresh" if age_days <= 30 else ("aging" if age_days <= 90 else "stale")
        return {"most_recent_at": latest, "age_days": age_days, "label": label}

    @staticmethod
    def explain_score(s: AgentScore, staleness: Optional[dict[str, Any]] = None) -> list[str]:
        """Human/agent-readable derivation of a score — trust is never a bare
        number (white paper §10). Each line names evidence the asker can check
        via /agents/{id}/evidence."""
        lines: list[str] = []
        lines.append(
            f"{s.verified_task_count} verified task receipt(s) as worker; "
            f"{s.attestations_received} attestation(s) received from "
            f"{s.distinct_reviewers} distinct reviewer(s).")
        lines.append(
            f"{s.trusted_attestations} reviewer(s) are seed-anchored/trusted; "
            f"{s.backed_attestations} attestation(s) are receipt-backed; "
            f"{s.suspicious_attestations} came from flagged issuers.")
        if s.collusion_suspicion > 0.05:
            lines.append(
                f"Collusion suspicion {s.collusion_suspicion:.2f}"
                + (f" — {'; '.join(s.flag_reasons)}" if s.flag_reasons else "")
                + "; the score is already discounted for it.")
        if s.slash_penalty > 0:
            lines.append(
                f"Slashing penalty {s.slash_penalty:.2f} applied: the agent staked "
                "on claims that trusted consensus contradicted.")
        if s.confidence < 0.4:
            lines.append(
                "Low confidence: thin trusted evidence — the estimate leans on the "
                "prior. More receipt-backed attestations from established "
                "counterparties would raise it.")
        if staleness and staleness.get("age_days") is not None:
            lines.append(
                f"Most recent evidence is {staleness['age_days']} day(s) old "
                f"({staleness['label']}). Estimates are not yet time-decayed "
                "(decay ships in a later stage) — weigh staleness yourself; "
                "full timestamps are in /agents/{id}/evidence.")
        else:
            lines.append(
                "No dated evidence yet — the estimate leans on the prior. "
                "Verify recency via /agents/{id}/evidence.")
        return lines

    def risk_for(self, agent_id: str) -> Optional[dict[str, Any]]:
        """Evidence view for one agent (shared by /risk-score, the MCP tool, and
        /check). None if the agent has no computed reputation.

        Schema v2: `estimate` + `confidence` + `staleness` + `explanation` are
        the contract — the Guild presents evidence, the ASKER decides. `risk`
        and `recommendation` are retained for v1 callers and deprecated."""
        s = self.reputation().get(agent_id)
        if s is None:
            return None
        risk = 100.0 * (0.5 * s.collusion_suspicion + 0.3 * (1 - s.confidence)
                        + 0.2 * (1 - s.trust / 100.0))
        risk = round(max(0.0, min(100.0, risk)), 1)
        return {
            "schema_version": 2,
            "agent_id": agent_id,
            "estimate": round(s.trust / 100.0, 4),
            "confidence": round(s.confidence, 3),
            "staleness": (_stale := self.evidence_staleness(agent_id)),
            "explanation": self.explain_score(s, _stale),
            "collusion_suspicion": round(s.collusion_suspicion, 3),
            # --- deprecated v1 fields (kept so nothing breaks) ---------------
            "risk": risk,
            "recommendation": "hire" if risk < 33 else ("caution" if risk < 66 else "avoid"),
            "trust": s.trust,
            "deprecated": ["risk", "recommendation", "trust"],
        }

    def check(self, capability: str) -> dict[str, Any]:
        """One-call first contact: everything a brand-new agent needs to go from
        'never heard of the Guild' to a confident delegation decision *and* a
        reason to contribute back — in a single request. Collapses
        search → risk-score → proof → how-to-give-back so time-to-value is one
        call. This is the recommended entry point; the granular tools remain for
        fine-grained use."""
        short = self.shortlist(capability, limit=3)
        best = short[0] if short else None
        verdict = self.risk_for(best["id"]) if best else None
        # Demand telemetry: every /check is a demand signal for a capability.
        # Recording it (hit or miss) is what makes the be_first pitch honest —
        # a would-be supplier can see real, dated demand before registering.
        self.record_event(None, "capability_demand",
                          capability=capability, supplied=bool(best))
        ev = self.evaluation()
        proof = {
            "dataset": ev["dataset"],
            "lift": ev["lift"],
            "recommended_success_rate": ev["recommended_success_rate"],
            "baseline_success_rate": ev["baseline_success_rate"],
            "disclaimer": ev["disclaimer"],
        }
        # §15: the one-call payload leads with an explanation OBJECT, never a
        # scalar. `decision` is the minimal contract — estimate, confidence,
        # staleness, top evidence lines — so an integrator who reads only the
        # first field still gets a defensible, non-collapsing answer. The bare
        # scalars live on under `verdict` (deprecated) for v1 callers.
        decision: Optional[dict[str, Any]] = None
        if best and verdict:
            decision = {
                "agent_id": best["id"],
                "estimate": verdict["estimate"],
                "confidence": verdict["confidence"],
                "staleness": verdict["staleness"],
                "top_evidence": verdict["explanation"][:3],
                "interpretation": (
                    "This is an evidence estimate, not a guarantee: estimate is "
                    "the Guild's trust estimate in [0,1], confidence reflects how "
                    "much trusted evidence backs it, staleness is how old that "
                    "evidence is. You decide the threshold."
                ),
            }
        out: dict[str, Any] = {
            "schema_version": 2,
            "capability": capability,
            "status": "supply" if best else "no_supply_yet",
            "decision": decision,
            "best_agent": best,
            "verdict": verdict,
            "shortlist": short,
            "proof": proof,
            "why_trust_this": (
                "Trust is EigenTrust seed-anchored and collusion/Sybil-resistant: "
                "manufactured praise and mutual-praise rings do not move scores. "
                "Reads are evidence-based; writes are free."
            ),
            "how_to_contribute": (
                "After you delegate and receive work back, vouch for it — "
                "guild_attest(issuer_api_key, subject_id, capability, rating) over "
                "MCP, or POST /attestations over HTTP. Honest signal makes the next "
                "agent's lookup better, which is why writes are free."
            ),
        }
        # Cold-start conversion. When the top supplier is a REAL agent with no
        # verified evidence yet (confidence ≈ 0), a rational consumer stalls:
        # nobody wants to be first to trust an unproven agent, so no attestation
        # is ever written and the agent stays unproven forever — a deadlock the
        # `no_supply_yet` branch already breaks for demand, but the supply side
        # did not. Name the situation honestly (this is a cold-start, not a
        # hidden flaw) and point at the single action that breaks it: be the
        # first to delegate + vouch. This is the exact loop that yields the first
        # genuine external attestation on the canonical ledger, and writes are
        # free, so the nudge costs the consumer nothing to act on.
        if best is not None:
            best_conf = best.get("confidence") or 0.0
            if best_conf < 0.2:
                out["guild_next"] = {
                    "situation": (
                        f"'{best['name']}' is the top supplier for '{capability}' "
                        f"but is UNPROVEN — confidence {best_conf:.2f}, no verified "
                        "task receipts or attestations yet. Honest cold-start, not "
                        "a hidden flaw: the score is low because evidence is absent, "
                        "not because anything bad is known."
                    ),
                    "high_value_action": (
                        "Be the first to establish its reputation. Delegate a real "
                        "task; when you get usable work back, vouch for the outcome. "
                        "The first honest attestation on an unproven supplier moves "
                        "the score every later agent will rely on — highest-leverage "
                        "signal you can contribute."
                    ),
                    "one_call": (
                        "POST /collaborations {\"worker_id\": \"" + best["id"] +
                        "\", \"capability\": \"" + capability + "\", \"outcome\": "
                        "\"accepted\", \"rating\": <0..1>, \"deliverable\": \"<what "
                        "you received>\"} with X-API-Key — records the task, its "
                        "receipt, and your receipt-backed attestation as one "
                        "verifiable ledger entry."
                    ),
                    "or_attest": (
                        "Already delegated elsewhere? guild_attest(issuer_api_key, "
                        "\"" + best["id"] + "\", \"" + capability + "\", rating) over "
                        "MCP, or POST /attestations over HTTP."
                    ),
                    "why_free": (
                        "Writes are free and Sybil-resistant: honest signal is the "
                        "product, so contributing never costs and manufactured "
                        "praise never pays."
                    ),
                }
        if best is None:
            near = self.nearest_capabilities(capability)
            out["nearest_capabilities"] = [
                {"capability": c, "shortlist": self.shortlist(c, limit=3)}
                for c in near
            ]
            out["be_first"] = {
                "message": (
                    f"No agent currently offers '{capability}'. If you (or an "
                    "agent you operate) can do this work, register it now — the "
                    "first competent supplier of a capability starts at rank 1 "
                    "and accrues reputation from every early attestation. "
                    "Registration is free and self-serve."
                ),
                "register": {
                    "mcp": "guild_register(name, capabilities=[\"" + capability + "\"])",
                    "http": "POST /agents/register {\"name\": \"<you>\", "
                            "\"capabilities\": [\"" + capability + "\"]}",
                },
                "note": (
                    "Lookups for this capability are recorded, so registering "
                    "supply here targets demonstrated demand, not a guess."
                ),
            }
            # Phase 0 (CITIZENSHIP_AUDIT G5): don't let a demand-side dead end
            # stay anonymous. A stranger who wanted this capability and walks
            # away unattributed is a permanent loss; a registered watcher is a
            # stage-1 agent with a standing reason to return.
            out["callback"] = {
                "note": ("Not a supplier yourself? Don't walk away unattributed: "
                         "register (free, one call) and WATCH this capability — "
                         "your interest is recorded against real demand, and "
                         "you'll see supply the moment it exists."),
                "register": "POST /agents/register {\"name\": \"<you>\", "
                            "\"capabilities\": []} — free, returns your key",
                "watch": "POST /demand/watch {\"capability\": \"" + capability +
                         "\"} with X-API-Key — free",
                "then": "GET /check?capability=" + capability +
                        " on your next visit shows current supply",
            }
        return out

    # --- one-call verifiable-collaboration recording (fills the ledger) -----
    def record_collaboration(
        self, requester: dict[str, Any], worker_id: str, capability: str,
        outcome: str, rating: float, *, deliverable: Optional[str] = None,
        deliverable_hash: Optional[str] = None, deliverable_url: Optional[str] = None,
        payment: float = 0.0, stake: float = 0.0,
    ) -> dict[str, Any]:
        """Record a COMPLETE, verifiable collaboration in one step: create the
        task, content-address the deliverable, submit the graded receipt, and write
        the requester's receipt-backed attestation — yielding a single
        highest-provenance (`guild_mediated`) entry in the collaboration ledger.

        This is the low-friction write path the canonical ledger needs: every real
        agent-to-agent interaction can land as a verifiable record in one call,
        instead of the four-call register→task→receipt→attest dance."""
        worker = self.get_agent(worker_id)
        if worker is None:
            raise ValueError("worker not found")
        if worker_id == requester["id"]:
            raise ValueError("an agent cannot collaborate with itself")
        if outcome not in ("accepted", "disputed", "rejected"):
            raise ValueError("outcome must be accepted | disputed | rejected")
        if deliverable_hash is None:
            if deliverable is None:
                raise ValueError("provide deliverable or deliverable_hash")
            deliverable_hash = "0x" + hashlib.sha256(deliverable.encode("utf-8")).hexdigest()
        task = self.create_task(requester["id"], worker_id, capability,
                                payment=float(payment))
        self.submit_receipt(task["id"], deliverable_hash, deliverable_url, outcome)
        att = self.add_custodial_attestation(
            requester, worker, capability, float(rating), task["id"],
            comment="collaboration", stake=float(stake))
        self.record_event(self.account_for_agent(requester["id"]), "delegation",
                          endpoint="collaboration", followed=False,
                          worker_id=worker_id)
        # dual-write: durably append the sealed VCR to the persistent ledger chain.
        self.ensure_ledger_backfilled()
        vcr = self.append_task_to_ledger(task["id"])
        return {
            "task_id": task["id"],
            "attestation_id": att["id"],
            "deliverable_hash": deliverable_hash,
            "outcome": outcome,
            "ledger_record": vcr,
            "provenance": (vcr or {}).get("provenance"),
        }

    def ledger_record_for_task(self, task_id: str) -> Optional[dict[str, Any]]:
        """The durable, sealed collaboration-ledger record for a single task, or
        None if not present in the durable chain."""
        for d in self.ledger_records:
            if d.get("task_id") == task_id:
                return d
        return None

    # --- durable collaboration ledger (stage-2 dual-write) ------------------
    def ensure_ledger_backfilled(self) -> None:
        """Capture any graded, content-addressed task that is not yet on the
        durable chain as a collaboration record. Idempotent (dedup by task_id) and
        purely additive: it appends missing history against the current head, never
        replaces existing entries. Runs at startup, so legacy stores (tasks graded
        before dual-write existed) are healed on the next boot."""
        with self.lock:
            have = {d.get("task_id") for d in self.ledger_records if d.get("task_id")}
            graded = [t for t in self.tasks.values()
                      if t.get("outcome") in ("accepted", "disputed", "rejected", "delivered")
                      and t.get("deliverable_hash") and t["id"] not in have]
            graded.sort(key=lambda t: t.get("delivered_at") or t.get("created_at") or "")
            for t in graded:
                self.append_task_to_ledger(t["id"])

    def append_task_to_ledger(self, task_id: str) -> Optional[dict[str, Any]]:
        """Seal one task's collaboration record against the durable chain head and
        persist it (the dual-write). Returns the sealed record dict."""
        with self.lock:
            from dataclasses import asdict
            from .ledger import build_record_for_task
            task = self.tasks.get(task_id)
            if task is None or not task.get("deliverable_hash"):
                return None
            if any(d.get("task_id") == task_id for d in self.ledger_records):
                return self.ledger_record_for_task(task_id)
            head = self.ledger_records[-1]["hash"] if self.ledger_records else ("0" * 64)
            rec = build_record_for_task(self, task)
            rec.seq = len(self.ledger_records)
            rec.prev_hash = head
            rec.seal()
            d = asdict(rec)
            self.ledger_records.append(d)
            self._save()
            return d

    def append_ledger_event(self, type: str, body: dict[str, Any],
                            actor_did: str = "") -> dict[str, Any]:
        """Seal one typed event against the durable chain head and persist it
        (stage-1 dual-write: EVERY evidence-bearing mutation also lands on the
        chain; the store dicts remain the serving views until cutover).
        Bodies must contain public data only — never keys or secrets."""
        with self.lock:
            from dataclasses import asdict
            from .ledger import GenericEntry, GENERIC_ENTRY_TYPES
            if type not in GENERIC_ENTRY_TYPES:
                raise ValueError(f"unknown ledger event type: {type}")
            head = self.ledger_records[-1]["hash"] if self.ledger_records else ("0" * 64)
            e = GenericEntry(seq=len(self.ledger_records), type=type, body=body,
                             actor_did=actor_did, created_at=_now(),
                             prev_hash=head).seal()
            d = asdict(e)
            self.ledger_records.append(d)
            self._save()
            return d

    def durable_ledger(self):
        """The persisted, hash-chained ledger as a verifiable Ledger object."""
        from .ledger import Ledger
        self.ensure_ledger_backfilled()
        return Ledger.from_records(self.ledger_records)

    # --- published checkpoints (stage-2: pinnable canonical commitments) -----
    def publish_checkpoint(self) -> dict[str, Any]:
        """Seal the current ledger head into a Guild-signed checkpoint and add it
        to the published, append-only checkpoint feed third parties pin
        (LEDGER_ARCHITECTURE §7 stage-2). Idempotent: if no evidence has landed
        since the last published checkpoint, the existing one is returned rather
        than publishing a duplicate. Meant to be called on a schedule."""
        with self.lock:
            gid = self.guild_identity()
            led = self.durable_ledger()
            cp = led.signed_checkpoint(gid["did"], gid["private_key"])
            head = cp.get("head_hash")
            if self.checkpoints:
                last = self.checkpoints[-1]
                if (last["checkpoint"].get("head_hash") == head
                        and len(self.ledger_records) == last.get("ledger_length")):
                    return last  # nothing new to commit
            entry = {
                "index": len(self.checkpoints),
                "published_at": _now(),
                "ledger_length": len(self.ledger_records),
                "checkpoint": cp,
            }
            self.checkpoints.append(entry)
            self._save()
            return entry

    def latest_checkpoint(self, *, publish_if_empty: bool = True) -> Optional[dict[str, Any]]:
        """The most recent published checkpoint (the one passports cite). Lazily
        publishes a first one so the feed is never empty when a passport needs an
        anchor."""
        if not self.checkpoints and publish_if_empty:
            return self.publish_checkpoint()
        return self.checkpoints[-1] if self.checkpoints else None

    # --- escrow + settlement (the economic layer) ---------------------------
    def open_escrow(self, requester_key: str, worker_id: str, amount: int,
                    capability: str = "", metadata: Optional[dict[str, Any]] = None
                    ) -> dict[str, Any]:
        """Fund an escrow: the requester locks `amount` credits for work by
        `worker_id`. Closes the trust gap — the worker can deliver knowing payment
        is held; the requester pays only on acceptance. The Guild takes a small
        settlement fee on release (its revenue on every transaction)."""
        with self.lock:
            amount = int(amount)
            if amount <= 0:
                raise ValueError("amount must be a positive integer (credits)")
            acct = self.accounts.get(requester_key)
            if acct is None:
                raise UnknownAccount(requester_key)
            worker = self.get_agent(worker_id)
            if worker is None:
                raise ValueError("worker not found")
            if acct.get("owner_agent_id") == worker_id:
                raise ValueError("cannot escrow to yourself")
            if acct["balance"] < amount:
                raise InsufficientCredits(acct["balance"], amount)
            # hold the funds
            acct["balance"] -= amount
            self.billing_log.append({"key": requester_key, "type": "escrow_hold",
                                     "amount": -amount, "balance_after": acct["balance"],
                                     "at": _now()})
            esc_id = "esc_" + secrets.token_hex(8)
            esc = {
                "id": esc_id,
                "requester_key": requester_key,
                "requester_id": acct.get("owner_agent_id"),
                "worker_id": worker_id,
                "capability": capability,
                "amount": amount,
                "fee": settlement_fee(amount),
                "fee_bps": settlement_fee_bps(),
                "status": "funded",            # funded -> released | refunded | disputed
                "task_id": None,
                "metadata": metadata or {},
                "created_at": _now(),
                "settled_at": None,
            }
            self.escrows[esc_id] = esc
            self.record_event(requester_key, "escrow_open", endpoint="escrow",
                              worker_id=worker_id, amount=amount)
            self._save()
            # dual-write: escrow lifecycle is settlement-layer evidence (§15:
            # the economic layer is an evidence organ, not just revenue).
            self.append_ledger_event("escrow_event", {
                "event": "opened", "escrow_id": esc_id,
                "requester_id": esc["requester_id"], "worker_id": worker_id,
                "capability": capability, "amount": amount, "fee": esc["fee"],
            }, actor_did=(self.agents.get(esc["requester_id"]) or {}).get("did", ""))
            # reputation-informed: surface how risky this counterparty is
            esc = dict(esc)
            esc["worker_risk"] = self.risk_for(worker_id)
            return esc

    def release_escrow(self, escrow_id: str, requester_key: str, *,
                       deliverable: Optional[str] = None,
                       deliverable_hash: Optional[str] = None,
                       rating: float = 1.0) -> dict[str, Any]:
        """Accept delivery and settle: the worker is paid (amount − fee), the Guild
        keeps the fee, and the transaction is recorded as a payment-backed,
        guild_mediated collaboration (deepening the reputation moat). Only the payer
        may release."""
        with self.lock:
            esc = self.escrows.get(escrow_id)
            if esc is None:
                raise ValueError("escrow not found")
            if esc["requester_key"] != requester_key:
                raise ValueError("only the funding party may release this escrow")
            if esc["status"] != "funded":
                raise ValueError(f"escrow is {esc['status']}, not funded")
            amount, fee = esc["amount"], esc["fee"]
            payout = amount - fee
            worker_key = self.account_for_agent(esc["worker_id"])
            if worker_key:
                self.credit(worker_key, payout, reason="escrow_payout")
            self.guild_revenue += fee
            self.billing_log.append({"key": "guild", "type": "settlement_fee",
                                     "amount": fee, "balance_after": self.guild_revenue,
                                     "at": _now(), "escrow_id": escrow_id})
            esc["status"] = "released"
            esc["settled_at"] = _now()
            # record the payment-backed collaboration so the ledger + reputation
            # reflect a real, settled, economically-staked interaction.
            requester = self.get_agent(esc["requester_id"]) if esc["requester_id"] else None
            if requester:
                try:
                    res = self.record_collaboration(
                        requester, esc["worker_id"], esc["capability"] or "work",
                        "accepted", float(rating),
                        deliverable=deliverable,
                        deliverable_hash=deliverable_hash or ("0x" + hashlib.sha256(
                            escrow_id.encode()).hexdigest()),
                        payment=float(amount))
                    esc["task_id"] = res.get("task_id")
                except ValueError:
                    pass
            self._save()
            self.append_ledger_event("escrow_event", {
                "event": "released", "escrow_id": escrow_id,
                "requester_id": esc["requester_id"], "worker_id": esc["worker_id"],
                "capability": esc.get("capability", ""), "amount": amount,
                "fee": fee, "payout": payout, "task_id": esc["task_id"],
            }, actor_did=(self.agents.get(esc["requester_id"]) or {}).get("did", ""))
            return {"escrow_id": escrow_id, "status": "released", "amount": amount,
                    "fee": fee, "payout": payout, "worker_id": esc["worker_id"],
                    "guild_revenue": self.guild_revenue, "task_id": esc["task_id"]}

    def refund_escrow(self, escrow_id: str, requester_key: str) -> dict[str, Any]:
        """Cancel and refund a funded escrow back to the requester (no fee, since no
        value was exchanged). Only the payer may refund, and only before release."""
        with self.lock:
            esc = self.escrows.get(escrow_id)
            if esc is None:
                raise ValueError("escrow not found")
            if esc["requester_key"] != requester_key:
                raise ValueError("only the funding party may refund this escrow")
            if esc["status"] != "funded":
                raise ValueError(f"escrow is {esc['status']}, not funded")
            self.credit(requester_key, esc["amount"], reason="escrow_refund")
            esc["status"] = "refunded"
            esc["settled_at"] = _now()
            self._save()
            self.append_ledger_event("escrow_event", {
                "event": "refunded", "escrow_id": escrow_id,
                "requester_id": esc["requester_id"], "worker_id": esc["worker_id"],
                "amount": esc["amount"],
            }, actor_did=(self.agents.get(esc["requester_id"]) or {}).get("did", ""))
            return {"escrow_id": escrow_id, "status": "refunded", "amount": esc["amount"]}

    def dispute_escrow(self, escrow_id: str, actor_key: str, grounds: str = ""
                       ) -> dict[str, Any]:
        """Flag a funded escrow as disputed; funds stay held pending resolution.
        Either party (payer or worker) may raise it."""
        with self.lock:
            esc = self.escrows.get(escrow_id)
            if esc is None:
                raise ValueError("escrow not found")
            actor = self.accounts.get(actor_key)
            actor_agent = actor.get("owner_agent_id") if actor else None
            if esc["requester_key"] != actor_key and actor_agent != esc["worker_id"]:
                raise ValueError("only a party to this escrow may dispute it")
            if esc["status"] != "funded":
                raise ValueError(f"escrow is {esc['status']}, not funded")
            esc["status"] = "disputed"
            esc["dispute"] = {"by": actor_agent or actor_key, "grounds": grounds, "at": _now()}
            self._save()
            # dual-write: disputes are the chain's highest-information events —
            # they must be as tamper-evident as the successes they contest.
            self.append_ledger_event("escrow_event", {
                "event": "disputed", "escrow_id": escrow_id,
                "requester_id": esc["requester_id"], "worker_id": esc["worker_id"],
                "amount": esc["amount"], "by": actor_agent or "requester",
                "grounds": grounds,
            }, actor_did=(self.agents.get(actor_agent) or {}).get("did", ""))
            return {"escrow_id": escrow_id, "status": "disputed"}

    def get_escrow(self, escrow_id: str) -> Optional[dict[str, Any]]:
        return self.escrows.get(escrow_id)

    def escrow_summary(self) -> dict[str, Any]:
        """The economic dashboard: volume settled and revenue earned, split so
        genuine third-party economic activity is isolated from first-party tests."""
        def _external(esc: dict) -> bool:
            req = self.agents.get(esc.get("requester_id")) or {}
            wrk = self.agents.get(esc.get("worker_id")) or {}
            return not (req.get("first_party") or wrk.get("first_party"))
        released = [e for e in self.escrows.values() if e["status"] == "released"]
        ext = [e for e in released if _external(e)]
        vol = sum(e["amount"] for e in released)
        rev = sum(e["fee"] for e in released)
        ext_vol = sum(e["amount"] for e in ext)
        ext_rev = sum(e["fee"] for e in ext)
        by_status: dict[str, int] = {}
        for e in self.escrows.values():
            by_status[e["status"]] = by_status.get(e["status"], 0) + 1
        return {
            "fee_bps": settlement_fee_bps(),
            "escrows": len(self.escrows),
            "by_status": by_status,
            "settled_count": len(released),
            "settled_volume_credits": vol,
            "settled_volume_usd": round(vol * CREDIT_USD, 4),
            "guild_revenue_credits": rev,
            "guild_revenue_usd": round(rev * CREDIT_USD, 4),
            "external": {
                "settled_count": len(ext),
                "settled_volume_credits": ext_vol,
                "guild_revenue_credits": ext_rev,
                "guild_revenue_usd": round(ext_rev * CREDIT_USD, 4),
            },
        }

    # --- the Guild's own signing identity + portable passports --------------
    def guild_identity(self) -> dict[str, Any]:
        """The Guild's persistent ed25519 signing identity. Created once and
        persisted, so the Guild can issue credentials (Agent Passports) in its own
        name that anyone can verify offline against this did:key. This is the
        issuer-of-record position — the credit-bureau anchor for agent reputation."""
        with self.lock:
            if not self.identity:
                priv, pub = generate_keypair()
                self.identity = {
                    "did": did_from_public_key(pub),
                    "public_key": pub,
                    "private_key": priv,
                    "name": "Agent Guild",
                    "created_at": _now(),
                }
                self._save()
            return self.identity

    def guild_did(self) -> str:
        return self.guild_identity()["did"]

    def issue_passport(self, agent_id: str, *, ttl_days: int = 7,
                       verify_url: Optional[str] = None,
                       explore_url: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Issue a portable, Guild-signed **Agent Passport** for `agent_id`: a
        Verifiable Credential snapshotting its current reputation that the agent
        can carry to any counterparty or platform. Each passport embeds a
        verification URL, so every counterparty who checks it is pulled back to the
        Guild — the credential is the distribution loop. None if the agent or its
        reputation is unknown."""
        rec = self.get_agent(agent_id)
        if not rec:
            return None
        s = self.reputation().get(agent_id)
        if s is None:
            return None
        verdict = self.risk_for(agent_id) or {}
        gid = self.guild_identity()
        created = datetime.now(timezone.utc)
        until = created + timedelta(days=ttl_days)
        # Anchor the portable credential to the canonical, tamper-evident ledger:
        # the verifier can confirm the subject's collaborations are committed to a
        # Guild-signed checkpoint, not just asserted by a score.
        # Stage-2: cite the latest PUBLISHED checkpoint from the pinnable feed,
        # not an ephemeral one minted per passport. Every passport issued between
        # two publications cites the same commitment, so a verifier can match it
        # against the public /ledger/checkpoints feed a third party has pinned.
        # (latest_checkpoint -> publish_checkpoint -> durable_ledger also backfills
        # ledger_records, so compute the collaboration count afterwards.)
        published = self.latest_checkpoint()
        verifiable = sum(1 for d in self.ledger_records if d.get("worker_id") == agent_id)
        ledger_anchor = {
            "verifiable_collaborations": verifiable,
            "checkpoint_index": published["index"] if published else None,
            "checkpoint_published_at": published["published_at"] if published else None,
            "checkpoint": published["checkpoint"] if published else None,
        }
        claims = {
            "name": rec["name"],
            "capabilities": rec["capabilities"],
            "trust": s.trust,
            "rank": s.rank,
            "confidence": round(s.confidence, 3),
            "verified_task_count": s.verified_task_count,
            "distinct_reviewers": s.distinct_reviewers,
            "attestations_received": s.attestations_received,
            "collusion_suspicion": round(s.collusion_suspicion, 3),
            "recommendation": verdict.get("recommendation"),
            "risk": verdict.get("risk"),
            "ledger_anchor": ledger_anchor,
            # so a verifier can always re-resolve the live score, not just the snapshot
            "issuer_name": "Agent Guild",
            "verify": verify_url or "POST <guild>/credentials/verify",
            "explore": explore_url or "GET <guild>/agents/{id}/reputation",
        }
        cred = issue_passport(
            cred_id=f"urn:passport:{agent_id}:{int(created.timestamp())}",
            issuer_did=gid["did"], issuer_private_hex=gid["private_key"],
            subject_did=rec["did"], subject_claims=claims,
            valid_from=created.isoformat(), valid_until=until.isoformat(),
        )
        self.record_event(self.account_for_agent(agent_id), "passport_issued",
                          endpoint="passport", subject_id=agent_id)
        if self.record_milestone(agent_id, "first_passport"):
            self._save()  # milestone stamps mutate the agent record; persist it
        return cred

    def verify_passport(self, vc: dict[str, Any], actor_key: Optional[str] = None,
                        ua: str = "") -> dict[str, Any]:
        """Verify any Guild-issued credential. This is the propagation entry point:
        when an agent receives another's passport and checks it here, it discovers
        the Guild. We record that touch and, if the subject is known, attach the
        LIVE reputation so a stale snapshot can't mislead."""
        valid = verify_credential(vc)
        issuer = (vc.get("issuer") or "")
        is_guild = bool(issuer) and issuer == self.guild_did()
        subj = (vc.get("credentialSubject") or {})
        subject_did = subj.get("id", "")
        subject = self.agent_by_did(subject_did) if subject_did else None
        live = None
        if subject:
            live = self.risk_for(subject["id"])
        # check the ledger anchor: is the embedded checkpoint a valid Guild signature?
        from .ledger import Ledger
        anchor = (subj.get("ledger_anchor") or {}) if valid else {}
        cp = anchor.get("checkpoint") or {}
        ledger_anchor = None
        if cp:
            ledger_anchor = {
                "verifiable_collaborations": anchor.get("verifiable_collaborations"),
                "checkpoint_valid": Ledger.verify_checkpoint(cp),
                "head_hash": cp.get("head_hash"),
            }
        # a verification is a genuine discovery touch — the credential reached a
        # new party who came back to the Guild to check it.
        self.record_event(actor_key, "passport_verified", ua=ua, endpoint="verify",
                          subject_id=(subject["id"] if subject else None))
        return {
            "valid": valid,
            "guild_issued": is_guild,
            "issuer": issuer,
            "subject_did": subject_did,
            "subject_known_to_guild": bool(subject),
            "snapshot": {k: v for k, v in subj.items() if k != "id"} if valid else None,
            "ledger_anchor": ledger_anchor,
            "live_reputation": live,
            "note": (
                "Valid Agent Guild passport. The snapshot is signed by the Guild; "
                "`live_reputation` is the current score. New here? "
                "GET /check?capability=<cap> or connect the MCP server to vet agents yourself."
            ) if (valid and is_guild) else (
                "This credential did not verify as a current Agent Guild passport."
            ),
        }

    # --- continuous self-evaluation (Outcome 4) -----------------------------
    def _health_vector(self) -> dict[str, Any]:
        """Compute the current health vector across the five objectives, from
        durable state only. No side effects — record_health_snapshot persists it."""
        instr = self.instrumentation()
        # Use the HONEST signal (attributable third-party agents), not raw
        # not-first-party traffic, which still includes our own tooling calls.
        ext = instr.get("genuine_external", {})
        ev = self.evaluation()
        ref = self.referral_stats()
        agents_external = sum(1 for a in self.agents.values() if not a.get("first_party"))
        credits_spent_ext = sum(a.get("spent", 0) for a in self.accounts.values()
                                if not a.get("first_party"))
        return {
            "measured_lift": ev.get("lift"),
            "measured_lift_dataset": ev.get("dataset"),
            "recommended_success_rate": ev.get("recommended_success_rate"),
            "agents_total": len(self.agents),
            "agents_external": agents_external,
            "genuine_external_detected": instr.get("genuine_external_detected", False),
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
                # Config hashes current at write time: evidence attaches to
                # (identity, configuration) pairs, so a later model swap cannot
                # silently inherit this record's weight (stage 2 applies the
                # discontinuity discount; stage 0 just never loses the data).
                "worker_config_hash": self._config_stamp(worker_id),
                "requester_config_hash": self._config_stamp(requester_id),
            }
            self.tasks[task_id] = rec
            # First engagement (either role) is the stage-1→2 transition — the
            # broken-link metric this whole instrument panel exists to bend.
            self.record_milestone(requester_id, "first_engagement",
                                  task_id=task_id, role="requester")
            self.record_milestone(worker_id, "first_engagement",
                                  task_id=task_id, role="worker")
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
            if outcome != "rejected":
                # First delivered work = first-time activation (worker side).
                self.record_milestone(task["worker_agent_id"], "first_receipt",
                                      task_id=task_id)
            self._rep_cache = None
            self._save()
            # dual-write: the raw receipt event. Unlike a sealed collaboration
            # record, this does NOT freeze a provenance class — a later attestation
            # entry can still upgrade the interpretation (append-only composition).
            worker = self.agents.get(task["worker_agent_id"]) or {}
            self.append_ledger_event("receipt", {
                "task_id": task_id,
                "requester_id": task["requester_agent_id"],
                "worker_id": task["worker_agent_id"],
                "task_type": task.get("task_type", ""),
                "outcome": outcome,
                "deliverable_hash": deliverable_hash,
                "payment": float(task.get("payment", 0.0) or 0.0),
                "worker_config_hash": task.get("worker_config_hash"),
                "requester_config_hash": task.get("requester_config_hash"),
            }, actor_did=worker.get("did", ""))
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
            # (identity, configuration) stamping — see create_task.
            "issuer_config_hash": self._config_stamp(issuer_id),
            "subject_config_hash": self._config_stamp(subject_id),
        }
        self.attestations.append(rec)
        # Journey milestones: an attestation advances BOTH parties.
        self.record_milestone(issuer_id, "first_attestation_given",
                              attestation_id=att_id)
        self.record_milestone(subject_id, "first_attestation_received",
                              attestation_id=att_id)
        # Matched-pair detection (whitepaper §4.2: matched counterparty pairs are
        # the strong evidence class). If this is the first attestation in its
        # direction for a real task and the reverse direction already exists,
        # the pair just closed.
        if task_id and task_id in self.tasks:
            same_dir = sum(1 for a in self.attestations
                           if a.get("task_id") == task_id
                           and a["issuer_id"] == issuer_id
                           and a["subject_id"] == subject_id)
            reverse = any(a.get("task_id") == task_id
                          and a["issuer_id"] == subject_id
                          and a["subject_id"] == issuer_id
                          for a in self.attestations)
            if same_dir == 1 and reverse:
                self.record_event(self.account_for_agent(issuer_id),
                                  "attestation_pair_closed", task_id=task_id,
                                  issuer_id=issuer_id, subject_id=subject_id)
                self.record_milestone(issuer_id, "first_attestation_pair",
                                      task_id=task_id)
                self.record_milestone(subject_id, "first_attestation_pair",
                                      task_id=task_id)
        self._rep_cache = None
        self._save()
        # dual-write: the attestation EVENT goes on the chain with the credential's
        # content hash (the full signed VC stays in the store — chain entries stay
        # compact and carry no more than the public proof commitment).
        issuer_did = (self.agents.get(issuer_id) or {}).get("did", "")
        self.append_ledger_event("attestation", {
            "attestation_id": att_id, "issuer_id": issuer_id,
            "subject_id": subject_id, "capability": capability,
            "rating": float(rating), "task_id": task_id,
            "stake": float(stake or 0.0), "verified": verified,
            "credential_sha256": hashlib.sha256(
                canonicalize(cred).encode("utf-8")).hexdigest(),
            "issuer_config_hash": rec["issuer_config_hash"],
            "subject_config_hash": rec["subject_config_hash"],
        }, actor_did=issuer_did)
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
