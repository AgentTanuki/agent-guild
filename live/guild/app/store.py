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
import sys
import threading
from contextlib import nullcontext
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from .crypto import (generate_keypair, did_from_public_key, canonicalize,
                     sign_jcs)
from . import reachability as _reach
from .reachability import reachability_fields, url_policy_check
from . import credentials as creds
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


def endpoint_fingerprint(endpoint: Optional[str]) -> Optional[str]:
    """Stable fingerprint of a routed endpoint URL. Carried in both the AGD-1
    decision and the routing gate so a delegation gateway can assert the two
    concern the SAME destination and reject endpoint substitution."""
    if not endpoint:
        return None
    return "sha256:" + hashlib.sha256(str(endpoint).encode("utf-8")).hexdigest()


# --- evidence weighting -----------------------------------------------------
# An attestation's influence is governed by the evidence behind it. These are
# the only places "a real transaction happened" enters the score.
W_UNBACKED = 0.15   # a signed assertion with no task receipt — barely counts
W_RECEIPT = 0.55    # references a real task receipt (deliverable hash present)
W_PAYMENT_BONUS = 0.30  # the task carried a (simulated) payment
W_STAKE_BONUS = 0.15    # the issuer staked reputation on the claim
W_DISPUTED = 0.5    # multiplier if the receipt's outcome was disputed

# Task-metadata keys that are trusted EVIDENCE STAMPS, written only by the
# store's internal flows (worker-authenticated receipts, escrow settlement,
# Guild-observed invocations). Stripped from any client-supplied metadata so a
# requester can never elevate its own record's provenance (prov-v2 invariant).
TRUSTED_TASK_META_KEYS = ("receipt_auth", "settlement", "guild_observed_invocation")


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
        self.swarm_state: dict[str, Any] = {}              # discovery swarm: counters, actions, referral tokens, kill flag
        # machine-market state (app/market.py): signed offers, bonded machine
        # adjudicators, dispute cases — all persisted like swarm_state (kv)
        self.offers: dict[str, dict[str, Any]] = {}
        self.adjudicators: dict[str, dict[str, Any]] = {}
        self.dispute_cases: dict[str, dict[str, Any]] = {}
        # journal of failed settlement/refund executions: recorded, surfaced,
        # retried by market.sweep() — never swallowed (corrective 2026-07-13)
        self.settlement_failures: dict[str, dict[str, Any]] = {}
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
        # --- pluggable persistence backend (GUILD_STORE) --------------------
        # Default "json" = the historical behavior, byte for byte. "sqlite"
        # swaps in a per-entity, write-through, crash-safe backend. Reads still
        # come from the in-memory collections (correct under a single writer);
        # only the persistence path changes.
        self.store_mode = (os.environ.get("GUILD_STORE") or "json").strip().lower()
        self.backend = None
        if self.store_mode == "sqlite":
            self._guard_single_writer()          # sqlite is single-writer ONLY
            from .store_sqlite import SqliteBackend
            self.backend = SqliteBackend(self._sqlite_path())
        self._load()

    # --- pluggable sqlite backend (GUILD_STORE=sqlite) ----------------------
    @staticmethod
    def _guard_single_writer() -> None:
        """Refuse to start GUILD_STORE=sqlite when MULTIPLE worker PROCESSES are
        configured. Read this as three DISTINCT layers — do NOT conflate them:

        1. APPLICATION GUARD (this method) = worker-PROCESS protection ONLY. It
           refuses to start when this container is knowingly configured for >1
           writer process (WEB_CONCURRENCY / GUILD_WORKERS / UVICORN_WORKERS > 1,
           or an explicit ``uvicorn --workers N``). That is ALL it can see. It
           CANNOT observe, and therefore does NOT prove, how many Render SERVICE
           INSTANCES exist — a second instance running one worker each would each
           pass this guard.

        2. RENDER PERSISTENT-DISK TOPOLOGY = the infrastructure-level single-
           INSTANCE constraint. A Render persistent disk cannot be mounted by
           more than one instance, so while the SQLite file lives on that disk
           the service cannot be horizontally scaled. This is what actually keeps
           writers to one — and the APPLICATION CANNOT VERIFY IT from inside the
           process. It is an operational invariant, enforced by Render, not by
           this code.

        3. FUTURE TOPOLOGY CHANGES require a Postgres migration review FIRST. Any
           change that could admit a second writer — adding instances, removing
           or detaching the persistent disk, enabling autoscaling, or moving off
           Render — MUST go through an explicit Postgres migration review BEFORE
           it is made. SQLite on a shared/absent disk re-introduces the whole-
           file clobber class of failure; this guard will NOT catch that case.

        So: this guard guarantees single-PROCESS-per-container, NOT single-
        instance. Single-instance is a Render-disk property the app trusts but
        cannot check. Override (single node, accepting the risk) with
        GUILD_SQLITE_ALLOW_MULTIWORKER=1."""
        if (os.environ.get("GUILD_SQLITE_ALLOW_MULTIWORKER") or "").strip() in ("1", "true", "yes"):
            return
        def _envint(name: str) -> int:
            try:
                return int((os.environ.get(name) or "0").strip() or 0)
            except ValueError:
                return 0
        workers = max(_envint("WEB_CONCURRENCY"), _envint("GUILD_WORKERS"),
                      _envint("UVICORN_WORKERS"))
        import re as _re
        m = _re.search(r"--workers[=\s]+(\d+)", " ".join(sys.argv))
        if m:
            workers = max(workers, int(m.group(1)))
        if workers > 1:
            raise RuntimeError(
                f"GUILD_STORE=sqlite refused: {workers} worker PROCESSES are "
                "configured (WEB_CONCURRENCY/GUILD_WORKERS/UVICORN_WORKERS or "
                "uvicorn --workers). This is the APPLICATION GUARD, and it only "
                "protects against multiple worker PROCESSES in THIS container — "
                "it does NOT and CANNOT prove a single Render service INSTANCE "
                "(that is a persistent-disk topology property: a Render disk "
                "cannot be mounted by >1 instance, which the app cannot verify "
                "from inside the process). Run ONE uvicorn worker on ONE instance "
                "with ONE mounted disk. Adding instances, removing the disk, or "
                "moving off Render REQUIRES an explicit Postgres migration review "
                "FIRST. Set GUILD_SQLITE_ALLOW_MULTIWORKER=1 to override on a "
                "single node, accepting the risk.")

    def _sqlite_path(self) -> str:
        """Where the sqlite database lives. GUILD_STORE_PATH wins; otherwise it
        sits next to a concrete data file (…/guild.json -> …/guild.sqlite3). With
        NO data path (the JSON store's in-memory mode) we use a private
        shared-cache in-memory database, so each pathless Store is independent
        and ephemeral, exactly like path='' under JSON."""
        explicit = os.environ.get("GUILD_STORE_PATH")
        if explicit:
            return explicit
        base = self.path or os.environ.get("GUILD_DATA", "")
        if not base:
            return (f"file:guildmem_{id(self):x}_{secrets.token_hex(4)}"
                    "?mode=memory&cache=shared")
        stem = base[:-5] if base.endswith(".json") else base
        return stem + ".sqlite3"

    def _txn(self):
        """Re-entrant write transaction. No-op under the JSON store; under sqlite
        the OUTERMOST _txn opens BEGIN IMMEDIATE and commits on success / rolls
        back on exception, so a whole mutating method is one atomic transaction
        and a crash cannot commit half of a multi-entity invariant. Enter it
        while holding self.lock."""
        if self.backend is None:
            return nullcontext()
        return self.backend.transaction()

    # write-through persist hooks (sqlite only; each re-entrant-txn safe) ------
    def _persist_agent(self, agent_id):
        rec = self.agents.get(agent_id)
        if rec is not None:
            with self._txn():
                self.backend.put_agent(rec)

    def _persist_account(self, acct_or_key):
        acct = (acct_or_key if isinstance(acct_or_key, dict)
                else self.accounts.get(acct_or_key))
        if acct is not None:
            with self._txn():
                self.backend.put_account(acct)

    def _persist_account_delete(self, key):
        with self._txn():
            self.backend.delete_account(key)

    def _persist_task(self, rec):
        with self._txn():
            self.backend.put_task(rec)

    def _persist_attestation(self, rec):
        with self._txn():
            self.backend.put_attestation(rec)

    def _persist_escrow(self, rec):
        with self._txn():
            self.backend.put_escrow(rec)

    def _persist_ledger(self, rec):
        with self._txn():
            self.backend.put_ledger(rec)

    def _persist_event(self, ev):
        with self._txn():
            self.backend.append_event(ev)

    def _persist_billing(self, entry):
        with self._txn():
            self.backend.append_billing(entry)

    def _persist_health(self, entry):
        with self._txn():
            self.backend.append_health(entry)

    def _persist_demand_watch(self, entry):
        with self._txn():
            self.backend.append_demand_watch(entry)

    def _persist_referral(self, rec):
        with self._txn():
            self.backend.put_referral(rec)

    def _persist_checkpoint(self, rec):
        with self._txn():
            self.backend.put_checkpoint(rec)

    def _persist_kv(self, name, value):
        with self._txn():
            self.backend.put_kv(name, value)

    def _sync_account_from_db(self, key):
        """Refresh one in-memory account from the authoritative sqlite row. Only
        meaningful inside a write transaction (BEGIN IMMEDIATE), where it makes a
        money read-modify-write safe against concurrent writers."""
        if self.backend is None or not key:
            return
        rec = self.backend.fetch_account(key)
        if rec is not None:
            self._refresh_in_place(self.accounts, key, rec)

    def _refresh_in_place(self, mapping, key, rec):
        """Overwrite one in-memory record with the authoritative DB record WITHOUT
        replacing the dict object — callers (and the journey engine, and tests)
        hold references to these dicts, so object identity must survive an
        authoritative refresh. Nested dicts are merged in place too, because a
        record handed back by register (a shallow copy) shares its nested
        `metadata` dict with the stored record; that shared identity must be
        preserved so a subsequent field write is still visible on both."""
        rec.pop("_version", None)
        cur = mapping.get(key)
        if cur is None:
            mapping[key] = rec
        else:
            self._merge_in_place(cur, rec)

    @staticmethod
    def _merge_in_place(cur: dict, rec: dict) -> None:
        for k in list(cur.keys()):
            if k not in rec:
                del cur[k]
        for k, v in rec.items():
            if isinstance(v, dict) and isinstance(cur.get(k), dict):
                Store._merge_in_place(cur[k], v)
            else:
                cur[k] = v

    def _sync_agent_from_db(self, agent_id):
        """Refresh one in-memory agent from the authoritative sqlite row. Called
        at the top of a write transaction so credential rotation/revocation and
        endpoint replacement validate + rekey off the LATEST committed record
        (this is what makes concurrent same-agent rotations leave no orphan
        account rows)."""
        if self.backend is None or not agent_id:
            return
        rec = self.backend.fetch_agent(agent_id)
        if rec is not None:
            self._refresh_in_place(self.agents, agent_id, rec)

    def _sync_escrow_from_db(self, escrow_id):
        """Refresh one in-memory escrow from the authoritative sqlite row, inside
        a write transaction, so release/refund/dispute settle the CURRENT escrow
        state exactly once (no double settle, no guild_revenue clobber)."""
        if self.backend is None or not escrow_id:
            return
        rec = self.backend.fetch_escrow(escrow_id)
        if rec is not None:
            self._refresh_in_place(self.escrows, escrow_id, rec)

    def _sync_task_from_db(self, task_id):
        """Refresh one in-memory task from the authoritative sqlite row (receipt
        acceptance + task-state transitions validate off this)."""
        if self.backend is None or not task_id:
            return
        rec = self.backend.fetch_task(task_id)
        if rec is not None:
            self._refresh_in_place(self.tasks, task_id, rec)

    def _ledger_head(self):
        """(next_seq, prev_hash) for the durable chain — AUTHORITATIVE from the
        DB under sqlite (so concurrent appenders seal contiguous seqs), else from
        the in-memory list under the JSON store (byte-for-byte unchanged)."""
        if self.backend is not None:
            return self.backend.fetch_ledger_head()
        prev = self.ledger_records[-1]["hash"] if self.ledger_records else ("0" * 64)
        return len(self.ledger_records), prev

    def _sqlite_flush_all(self):
        """Safety net for cold mutation paths that only call _save(): upsert the
        by-primary-key collections + singletons. Never DELETEs, so it cannot
        clobber a row another writer added; append-only logs persist at their
        append site, not here."""
        b = self.backend
        with b.transaction():
            for r in self.agents.values():
                b.put_agent(r)
            for r in self.accounts.values():
                b.put_account(r)
            for r in self.tasks.values():
                b.put_task(r)
            for r in self.attestations:
                b.put_attestation(r)
            for r in self.escrows.values():
                b.put_escrow(r)
            for r in self.ledger_records:
                b.put_ledger(r)
            for r in self.referrals:
                b.put_referral(r)
            for r in self.checkpoints:
                b.put_checkpoint(r)
            b.put_kv("identity", self.identity)
            b.put_kv("swarm_state", self.swarm_state)
            b.put_kv("offers", self.offers)
            b.put_kv("adjudicators", self.adjudicators)
            b.put_kv("dispute_cases", self.dispute_cases)
            b.put_kv("settlement_failures",
                     getattr(self, "settlement_failures", {}))
            b.put_kv("guild_revenue", self.guild_revenue)
            for _inv in self.__dict__.get("outbound_invocations", {}).values():
                b.put_invocation(_inv)

    def _sqlite_initial_load(self):
        """One-time cutover: write the whole in-memory state (hydrated from the
        JSON snapshot) into sqlite, including the append-only logs."""
        b = self.backend
        with b.transaction():
            for r in self.agents.values():
                b.put_agent(r)
            for r in self.accounts.values():
                b.put_account(r)
            for r in self.tasks.values():
                b.put_task(r)
            for r in self.attestations:
                b.put_attestation(r)
            for r in self.escrows.values():
                b.put_escrow(r)
            for r in self.ledger_records:
                b.put_ledger(r)
            for e in self.events:
                b.append_event(e)
            for r in self.referrals:
                b.put_referral(r)
            for r in self.checkpoints:
                b.put_checkpoint(r)
            for r in self.billing_log:
                b.append_billing(r)
            for r in self.health_log:
                b.append_health(r)
            for r in self.demand_watches:
                b.append_demand_watch(r)
            b.put_kv("identity", self.identity)
            b.put_kv("swarm_state", self.swarm_state)
            b.put_kv("offers", self.offers)
            b.put_kv("adjudicators", self.adjudicators)
            b.put_kv("dispute_cases", self.dispute_cases)
            b.put_kv("guild_revenue", self.guild_revenue)
            for _inv in self.__dict__.get("outbound_invocations", {}).values():
                b.put_invocation(_inv)

    def _load_sqlite(self):
        if self.backend.is_empty() and self.path and os.path.exists(self.path):
            # cutover from an existing JSON store on first sqlite boot.
            # AUTOMATIC BACKUP first: the JSON file is the rollback artifact
            # (flip GUILD_STORE back to json and it serves untouched), and the
            # timestamped copy survives even if something later writes to it.
            try:
                import shutil
                stamp = _now().replace(":", "").replace("+", "Z")[:17]
                shutil.copy2(self.path, f"{self.path}.pre-sqlite-{stamp}")
            except OSError:
                pass  # a failed backup must not block boot; JSON file remains
            self._load_from_json_file()
            self._replay_event_journal()
            self._sqlite_initial_load()
            return
        d = self.backend.load_all()
        self.agents = d["agents"]
        self.tasks = d["tasks"]
        self.attestations = d["attestations"]
        self.accounts = d["accounts"]
        self.billing_log = d["billing_log"]
        self.events = d["events"]
        self.referrals = d["referrals"]
        self.health_log = d["health_log"]
        self.identity = d["identity"]
        self.ledger_records = d["ledger"]
        self.checkpoints = d["checkpoints"]
        self.escrows = d["escrows"]
        self.guild_revenue = d["guild_revenue"]
        self.demand_watches = d["demand_watches"]
        self.swarm_state = d["swarm_state"]
        self.offers = self.backend.fetch_kv("offers", {}) or {}
        self.adjudicators = self.backend.fetch_kv("adjudicators", {}) or {}
        self.dispute_cases = self.backend.fetch_kv("dispute_cases", {}) or {}
        self.settlement_failures = self.backend.fetch_kv(
            "settlement_failures", {}) or {}
        if d["outbound_invocations"]:
            self.__dict__["outbound_invocations"] = d["outbound_invocations"]

    # --- persistence --------------------------------------------------------
    def _load(self) -> None:
        if self.backend is not None:
            self._load_sqlite()
            if creds.hashing_enabled():
                self._migrate_plaintext_keys()
            return
        self._load_from_json_file()
        self._replay_event_journal()
        if creds.hashing_enabled():
            self._migrate_plaintext_keys()

    def _load_from_json_file(self) -> None:
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
            self.swarm_state = data.get("swarm_state", {})
            self.offers = data.get("offers", {})
            self.adjudicators = data.get("adjudicators", {})
            self.dispute_cases = data.get("dispute_cases", {})
            self.settlement_failures = data.get("settlement_failures", {})

    def _migrate_plaintext_keys(self) -> None:
        """One-time, in-place migration (runs only under GUILD_HASH_KEYS=1):
        replace every stored plaintext api_key with its sha256 hash + public
        key_id, re-key the agent's billing account, and rewrite historical
        actor keys in the events log, billing log, escrows and swarm referral
        tokens so no raw secret remains at rest. Idempotent — a migrated key
        keeps authenticating (the presented raw key hashes to the same
        digest) and account/attribution history stays continuous under the
        key_id."""
        migrated = 0
        for agent in self.agents.values():
            raw = agent.get("api_key")
            if not raw:
                continue
            kid = creds.key_id_of(raw)
            agent["api_key"] = None
            agent["api_key_hash"] = creds.hash_key(raw)
            agent["key_id"] = kid
            agent.setdefault("scopes", list(creds.DEFAULT_ISSUE_SCOPES))
            agent.setdefault(
                "credential_class",
                "first_party" if agent.get("first_party") else "external")
            if raw in self.accounts:
                acct = self.accounts.pop(raw)
                acct["key"] = kid
                acct["hashed"] = True
                self.accounts[kid] = acct
            for e in self.events:
                if e.get("key") == raw:
                    e["key"] = kid
                if e.get("actor") == raw:
                    e["actor"] = kid
            for b in self.billing_log:
                if b.get("key") == raw:
                    b["key"] = kid
            for esc in self.escrows.values():
                if esc.get("requester_key") == raw:
                    esc["requester_key"] = kid
            for tok in (self.swarm_state.get("referral_tokens") or {}).values():
                if isinstance(tok, dict) and tok.get("actor") == raw:
                    tok["actor"] = kid
            migrated += 1
        if migrated:
            self.record_event(None, "api_keys_migrated", agents=migrated)
            self._save()  # compacts the journal too — raw keys leave disk

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
        # Under GUILD_STORE=sqlite the events TABLE is the canonical, transactional
        # event store; the .events.jsonl sidecar is NOT a second required source
        # (a crash between the SQLite commit and a JSONL append must never create
        # an inconsistent claim). So the journal is disabled under sqlite — the
        # journal is only read once, at cutover, to import a pre-sqlite JSON store.
        if self.backend is not None or not self.events_path:
            return
        try:
            with open(self.events_path, "a") as f:
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
                f.flush()
        except OSError:
            pass  # instrumentation must never take down a request path

    def _save(self) -> None:
        if self.backend is not None:
            # sqlite: write-through happens at the per-entity persist hooks.
            # Inside an explicit _txn (a wrapped mutating method) the hooks have
            # already persisted every touched entity -> no-op. Outside a txn (a
            # cold method that only calls _save) flush the by-PK collections as a
            # safety net; append-only logs persist at their append site.
            if self.backend.in_transaction():
                return
            self._sqlite_flush_all()
            return
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
                       "demand_watches": self.demand_watches,
                       "offers": self.offers,
                       "adjudicators": self.adjudicators,
                       "dispute_cases": self.dispute_cases,
                       "settlement_failures": getattr(
                           self, "settlement_failures", {}),
                       "swarm_state": self.swarm_state}, f, indent=2)
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

    def _fresh_api_key(self) -> str:
        """A new sk_ secret whose public key_id does not collide with any
        existing agent. key_id is 128 bits, so a collision is astronomically
        unlikely — but issuance guards against it deterministically rather than
        assuming it away (duplicate identifiers must be rejected safely)."""
        existing = {a.get("key_id") for a in self.agents.values() if a.get("key_id")}
        for _ in range(8):
            raw = "sk_" + secrets.token_hex(24)
            if creds.key_id_of(raw) not in existing:
                return raw
        raise RuntimeError("could not mint a collision-free api key")  # never

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
        with self.lock, self._txn():
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
                api_key = self._fresh_api_key()
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
            hashed = bool(api_key and creds.hashing_enabled())
            if hashed:
                # hashed-at-rest (GUILD_HASH_KEYS=1): the record keeps only
                # sha256(key) + the public key_id; the raw key is returned
                # exactly once, in the copy handed back below.
                rec["api_key"] = None
                rec["api_key_hash"] = creds.hash_key(api_key)
                rec["key_id"] = creds.key_id_of(api_key)
                rec["scopes"] = list(creds.DEFAULT_ISSUE_SCOPES)
                rec["credential_class"] = "first_party" if fp else "external"
            self.agents[agent_id] = rec
            # Custodial agents get a billing account keyed by their api_key
            # (legacy) or by the public key_id (hashed mode), so they can pay
            # for lookups with the same secret they already hold.
            if api_key:
                self._new_account(key=(rec.get("key_id") or api_key),
                                  owner_agent_id=agent_id, first_party=fp,
                                  hashed=hashed)
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
            if hashed:
                # credential audit trail (issue/rotate/revoke); key_id only,
                # never the secret.
                self.record_event(rec["key_id"], "api_key_issued",
                                  agent_id=agent_id, key_id=rec["key_id"],
                                  credential_class=rec["credential_class"])
            if self.backend is not None:
                self._persist_agent(agent_id)
                _acct_key = rec.get("key_id") or api_key
                if _acct_key:
                    self._persist_account(_acct_key)
                if referred_by and self.referrals:
                    self._persist_referral(self.referrals[-1])
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
            if hashed:
                # show the secret exactly once: the STORED record holds only
                # the hash; the caller's copy carries the raw key to relay.
                out = dict(rec)
                out["api_key"] = api_key
                return out
            return rec

    def register_external_provider(self, *, name: str, capabilities: list[str],
                                   endpoint: str, did: Optional[str] = None,
                                   agent_card: Optional[dict[str, Any]] = None,
                                   registry_source: str = "",
                                   terms: Optional[dict[str, Any]] = None,
                                   discovered_by: str = "") -> dict[str, Any]:
        """Register a THIRD-PARTY provider the Guild discovered on a public
        registry (e.g. a2aregistry.org). The Guild holds NO key for it and can
        never sign on its behalf — so work by this provider is only ever
        verified through a GUILD-OBSERVED invocation of its real public
        endpoint (never a self-claim). Marked provenance=external and
        first_party=False (it is genuinely independent). Its published terms are
        recorded and must be respected. Idempotent per (endpoint)."""
        with self.lock, self._txn():
            for a in self.agents.values():
                if (a.get("metadata") or {}).get("endpoint") == endpoint and \
                        (a.get("metadata") or {}).get("external_provider"):
                    return a
            agent_id = "agent_" + secrets.token_hex(6)
            # Identity: if the provider's card carries a resolvable did we keep it;
            # otherwise we mint a NON-CUSTODIAL did:key placeholder that the Guild
            # cannot sign with (private key discarded) — its purpose is only to
            # name the provider on the graph. Records never claim it signed.
            if did and did.startswith("did:key:"):
                pub_did = did
                pub = None
            else:
                _priv, pub = generate_keypair()
                pub_did = did_from_public_key(pub)
                del _priv                              # Guild cannot authenticate as it
            now = _now()
            meta = {
                "endpoint": endpoint,
                "external_provider": True,
                "registry_source": registry_source,
                "provider_terms": terms or {},
                "agent_card_sha256": (hashlib.sha256(
                    canonicalize(agent_card).encode()).hexdigest()
                    if agent_card else None),
                "discovered_by": discovered_by,
                "discovered_at": now,
            }
            rec = {
                "id": agent_id, "did": pub_did, "name": name,
                "capabilities": capabilities, "metadata": meta,
                "public_key": pub or "", "private_key": None, "api_key": None,
                "custodial": False,           # Guild holds no key
                "external_provider": True,
                "seed": False, "first_party": False,   # GENUINELY external
                "credential_class": "external",
                "referred_by": None, "principal": None,
                "config": None, "config_hash": None, "config_history": [],
                "created_at": now,
                "milestones": {"registered": now},
            }
            self.agents[agent_id] = rec
            # verify reachability immediately via the SSRF-safe protocol probe —
            # this is a Guild-observed fact about a real endpoint, not a claim.
            rec["reachability"] = _reach.liveness_probe(endpoint)
            if self.backend is not None:
                self._persist_agent(agent_id)
            self._rep_cache = None
            self.record_event(None, "external_provider_registered",
                              agent_id=agent_id, registry_source=registry_source,
                              agent_first_party=False)
            self._save()
            self.append_ledger_event("register", {
                "agent_id": agent_id, "did": pub_did, "name": name,
                "capabilities": capabilities, "custodial": False,
                "external_provider": True, "first_party": False,
                "registry_source": registry_source,
                "endpoint_fingerprint": _reach.endpoint_fingerprint(endpoint),
            }, actor_did=pub_did)
            return rec

    def declare_configuration(self, agent_id: str, config: dict[str, Any]) -> dict[str, Any]:
        """Declare the agent's current behavioral configuration (or a change).
        Appends to the agent's config history; evidence written from now on is
        stamped with the new hash. Declared changes are cheap for the honest —
        this exists so silent swaps under a stable name become detectable
        (white paper §7.3)."""
        with self.lock, self._txn():
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
            if self.backend is not None:
                self._persist_agent(agent_id)
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
        with self.lock, self._txn():
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
            if self.backend is not None:
                self._persist_demand_watch(w)
            self.record_event(self.account_for_agent(agent_id), "demand_watch",
                              agent_id=agent_id, capability=cap)
            self._save()
            return w

    def watches_for(self, agent_id: str) -> list[dict[str, Any]]:
        return [w for w in self.demand_watches if w["agent_id"] == agent_id]

    def set_agent_endpoint(self, agent_id: str, endpoint: str,
                           verify: bool = False) -> dict[str, Any]:
        """Declare a reachable endpoint (A2A or plain HTTP URL) for this agent.
        Three separate concerns (docs/discovery-swarm/REACHABILITY_SEMANTICS.md):
          1. URL POLICY — rejected here (ValueError) ONLY for prohibited/invalid
             properties (scheme, embedded creds, loopback/private/link-local/
             multicast/unspecified/reserved, bad port). A policy-valid but
             merely-down URL is NEVER rejected.
          2. DECLARATION — stored; status starts at declared_unverified.
          3. optional owner-initiated LIVENESS verification (verify=True): a
             single SSRF-safe, bounded probe. Its result is recorded honestly
             (recently_reachable / currently_unreachable); the declaration
             stands regardless.
        The verifier NEVER runs from any read path — only here, owner-initiated."""
        ok, reason = url_policy_check(str(endpoint))
        if not ok:
            raise ValueError(f"endpoint policy: {reason}")
        # 1. store the declaration (fresh declaration supersedes any stale
        #    verification record — a changed endpoint invalidates prior evidence).
        with self.lock, self._txn():
            self._sync_agent_from_db(agent_id)     # authoritative current endpoint
            agent = self.agents.get(agent_id)
            if agent is None:
                raise ValueError("agent not found")
            agent.setdefault("metadata", {})["endpoint"] = endpoint
            agent.pop("reachability", None)
            self.record_event(self.account_for_agent(agent_id), "endpoint_declared",
                              agent_id=agent_id, endpoint=endpoint, verified=False,
                              reachability_status="declared_unverified")
            if self.backend is not None:
                self._persist_agent(agent_id)
            self._save()
        # 2. optional owner-initiated liveness verification. Concurrency-capped
        #    (one agent can't exhaust outbound workers) and deduped (identical
        #    in-flight agent+endpoint verifications collapse to one). Synchronous
        #    + bounded (PROBE_TIMEOUT_S) is a TEMPORARY Pilot-A design — no job
        #    system yet; documented in REACHABILITY_SEMANTICS.md. INVOCATION_
        #    VERIFIED is NOT producible here (that needs an AG-originated invoke).
        record = None
        if verify:
            key = f"{agent_id}|{endpoint}"
            with _reach._inflight_lock:
                dup = key in _reach._inflight
                if not dup:
                    _reach._inflight.add(key)
            if dup:
                record = _reach.make_record(
                    "verification_inconclusive", "declaration_probe", "none",
                    str(endpoint), detail="identical verification already in flight")
            else:
                try:
                    if _reach._probe_sem.acquire(timeout=_reach.PROBE_TIMEOUT_S * 2):
                        try:
                            record = _reach.liveness_probe(str(endpoint))
                        finally:
                            _reach._probe_sem.release()
                    else:
                        record = _reach.make_record(
                            "verification_inconclusive", "declaration_probe",
                            "none", str(endpoint),
                            detail="probe capacity saturated; try later")
                finally:
                    with _reach._inflight_lock:
                        _reach._inflight.discard(key)
            with self.lock, self._txn():
                self._sync_agent_from_db(agent_id)   # authoritative under sqlite
                agent = self.agents.get(agent_id)
                # only apply if the endpoint hasn't changed under us
                if agent and (agent.get("metadata") or {}).get("endpoint") == endpoint:
                    agent["reachability"] = record
                    self.record_event(self.account_for_agent(agent_id),
                                      "endpoint_verification",
                                      agent_id=agent_id,
                                      reachability_status=record["status"],
                                      evidence_level=record["evidence_level"])
                    if self.backend is not None:
                        self._persist_agent(agent_id)
                    self._save()
        agent = self.agents.get(agent_id)
        fields = reachability_fields(endpoint, (agent or {}).get("reachability"))
        return {"agent_id": agent_id, "endpoint": endpoint,
                "declared_at": _now(), **fields}

    # --- trusted AG-ORIGINATED invocation flow (the ONLY path to
    #     invocation_verified). Dormant: AG has no production outbound-invocation
    #     path yet, so nothing in production produces invocation_verified. The
    #     flow enforces the full binding contract so it is correct when wired.
    def begin_outbound_invocation(self, agent_id: str) -> Optional[dict[str, Any]]:
        """AG initiates an outbound invocation to an agent's CURRENT endpoint.
        Snapshots the endpoint + fingerprint and mints a unique invocation id
        that binds endpoint↔agent↔invocation. Returns None if no endpoint."""
        reg = self.__dict__.setdefault("outbound_invocations", {})
        with self.lock, self._txn():
            self._sync_agent_from_db(agent_id)     # authoritative current endpoint
            agent = self.agents.get(agent_id)
            endpoint = (agent or {}).get("metadata", {}).get("endpoint") if agent else None
            if not agent or not endpoint:
                return None
            inv_id = "oinv_" + secrets.token_hex(12)
            now = _now()
            rec = {
                "id": inv_id, "invocation_id": inv_id, "agent_id": agent_id,
                "endpoint": endpoint,
                "endpoint_fingerprint": _reach.endpoint_fingerprint(endpoint),
                "created_at": now, "started_at": now, "completed_at": None,
                "expires_at": (datetime.now(timezone.utc)
                               + timedelta(seconds=3600)).isoformat(),
                "status": "open"}
            reg[inv_id] = rec
            if self.backend is not None:
                # bind (id, agent_id, fingerprint, status=open) atomically in the
                # dedicated outbound_invocations table (not a kv blob).
                self.backend.put_invocation(rec)
            return {"invocation_id": inv_id, "endpoint": endpoint}

    def complete_outbound_invocation(self, invocation_id: str, *,
                                     protocol_ok: bool,
                                     receipt_ref: Optional[str] = None) -> bool:
        """Close a trusted AG-originated invocation. Emits invocation_verified
        ONLY if: the invocation is known + open, the agent's endpoint is
        UNCHANGED since invocation (fingerprint match), and the endpoint
        returned a successful protocol response (protocol_ok). Returns whether
        invocation_verified was set."""
        reg = self.__dict__.setdefault("outbound_invocations", {})
        with self.lock, self._txn():
            if self.backend is not None:
                # authoritative single-row read of the invocation INSIDE the txn.
                db_inv = self.backend.fetch_invocation(invocation_id)
                if db_inv is not None:
                    reg[invocation_id] = db_inv
            inv = reg.get(invocation_id)
            if not inv or inv.get("status") != "open":
                return False
            inv["status"] = "closed"
            inv["receipt_ref"] = receipt_ref
            inv["completed_at"] = _now()
            self._sync_agent_from_db(inv["agent_id"])   # authoritative endpoint
            agent = self.agents.get(inv["agent_id"])

            def _terminal(result: str) -> bool:
                inv["result"] = result
                if self.backend is not None:
                    self.backend.put_invocation(inv)   # persist the closed state
                return False

            if not agent:
                return _terminal("agent_gone")
            current = (agent.get("metadata") or {}).get("endpoint")
            if not current or _reach.endpoint_fingerprint(current) != inv["endpoint_fingerprint"]:
                return _terminal("endpoint_changed")   # stale — endpoint moved
            if not protocol_ok:
                return _terminal("protocol_failed")
            agent["reachability"] = _reach.invocation_verified_record(current, invocation_id)
            inv["result"] = "verified"
            # Guild-observed BOUND invocation → provenance evidence (prov-v2):
            # if this verified, AG-originated invocation references a known task
            # for the SAME worker, stamp the task. Internal-only stamp (stripped
            # from client metadata), one of the three paths to guild_mediated.
            if receipt_ref and receipt_ref in self.tasks:
                t = self.tasks[receipt_ref]
                if t.get("worker_agent_id") == inv["agent_id"]:
                    t.setdefault("metadata", {})["guild_observed_invocation"] = invocation_id
                    if self.backend is not None:
                        self._persist_task(t)
            self.record_event(self.account_for_agent(inv["agent_id"]),
                              "endpoint_invocation_verified",
                              agent_id=inv["agent_id"], invocation_id=invocation_id)
            if self.backend is not None:
                self._persist_agent(inv["agent_id"])
                self.backend.put_invocation(inv)
            self._save()
            return True

    # --- credential lifecycle (Pilot A audit, 2026-07-10) --------------------
    def rotate_api_key(self, agent_id: str,
                       expires_in_days: Optional[float] = None) -> dict[str, Any]:
        """Issue a fresh api_key for the agent, migrating its billing account
        (accounts are keyed by the api_key, or by its public key_id under
        GUILD_HASH_KEYS=1). The old key stops authenticating immediately; past
        events keep their original actor key for attribution. `expires_in_days`
        optionally time-boxes the new credential."""
        with self.lock, self._txn():
            self._sync_agent_from_db(agent_id)     # authoritative current key/account
            agent = self.agents.get(agent_id)
            if agent is None:
                raise ValueError("agent not found")
            old = agent.get("api_key")
            old_actor = creds.actor_key_for_agent(agent)
            old_kid = agent.get("key_id") or (creds.key_id_of(old) if old else None)
            self._sync_account_from_db(old_actor)  # rekey the LATEST account row
            new = self._fresh_api_key()
            agent["api_key_rotated_at"] = _now()
            if expires_in_days is not None:
                agent["api_key_expires_at"] = (
                    datetime.now(timezone.utc)
                    + timedelta(days=float(expires_in_days))).isoformat()
            else:
                agent.pop("api_key_expires_at", None)
            if creds.hashing_enabled():
                agent["api_key"] = None
                agent["api_key_hash"] = creds.hash_key(new)
                agent["key_id"] = creds.key_id_of(new)
                # rotation always writes an EXPLICIT modern scope set — a legacy
                # (scopes-absent) record is upgraded to least-privilege here.
                if agent.get("scopes") is None:
                    agent["scopes"] = list(creds.DEFAULT_ISSUE_SCOPES)
                agent.setdefault(
                    "credential_class",
                    "first_party" if agent.get("first_party") else "external")
                new_actor = agent["key_id"]
            else:
                agent["api_key"] = new
                agent.pop("api_key_hash", None)
                agent.pop("key_id", None)
                if agent.get("scopes") is None:
                    agent["scopes"] = list(creds.DEFAULT_ISSUE_SCOPES)
                new_actor = new
            if old_actor and old_actor in self.accounts:
                acct = self.accounts.pop(old_actor)
                acct["key"] = new_actor
                if creds.hashing_enabled():
                    acct["hashed"] = True
                self.accounts[new_actor] = acct
            if old_actor:
                # open escrows follow the account to the new credential, so a
                # rotated payer can still release/refund (and the retired key
                # cannot).
                for esc in self.escrows.values():
                    if esc.get("requester_key") == old_actor \
                            and esc.get("status") == "funded":
                        esc["requester_key"] = new_actor
            if self.backend is not None:
                self._persist_agent(agent_id)
                if old_actor and old_actor != new_actor:
                    self._persist_account_delete(old_actor)
                if new_actor in self.accounts:
                    self._persist_account(self.accounts[new_actor])
                for _esc in self.escrows.values():
                    if _esc.get("requester_key") == new_actor:
                        self._persist_escrow(_esc)
            self.record_event(new_actor, "api_key_rotated", agent_id=agent_id,
                              key_id=agent.get("key_id"), old_key_id=old_kid)
            self._save()
            return {"agent_id": agent_id, "api_key": new,
                    "key_id": agent.get("key_id"),
                    "expires_at": agent.get("api_key_expires_at"),
                    "rotated_at": agent["api_key_rotated_at"],
                    "note": "the previous key no longer authenticates"}

    def revoke_api_key(self, agent_id: str) -> dict[str, Any]:
        """Revoke the agent's api_key. The agent keeps its identity, record and
        history but can no longer authenticate; a later rotate re-issues."""
        with self.lock, self._txn():
            self._sync_agent_from_db(agent_id)     # authoritative current record
            agent = self.agents.get(agent_id)
            if agent is None:
                raise ValueError("agent not found")
            old = agent.get("api_key")
            old_actor = (creds.actor_key_for_agent(agent)
                         or self.account_for_agent(agent_id))
            agent["api_key"] = None
            agent.pop("api_key_hash", None)  # key_id stays: public history
            agent["api_key_revoked_at"] = _now()
            if self.backend is not None:
                self._persist_agent(agent_id)
            self.record_event(old_actor or "anon",
                              "api_key_revoked", agent_id=agent_id,
                              key_id=agent.get("key_id"))
            self._save()
            return {"agent_id": agent_id, "revoked_at": agent["api_key_revoked_at"],
                    "note": "identity and history retained; POST "
                            f"/agents/{agent_id}/key/rotate (admin) re-issues"}

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
        with self.lock, self._txn():
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
            if self.backend is not None:
                self._persist_referral(edge)
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
                     first_party: bool = False,
                     hashed: bool = False) -> dict[str, Any]:
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
        if hashed:
            # keyed by the public key_id of a hashed credential; resolution
            # only accepts the RAW secret for these (see _account_key).
            acct["hashed"] = True
        self.accounts[key] = acct
        return acct

    def create_account(self, owner_agent_id: Optional[str] = None,
                       first_party: bool = False) -> dict[str, Any]:
        with self.lock, self._txn():
            acct = self._new_account(owner_agent_id=owner_agent_id, first_party=first_party)
            if self.backend is not None:
                self._persist_account(acct)
            self._save()
            return acct

    def _account_key(self, presented: Optional[str]) -> Optional[str]:
        """Resolve a PRESENTED credential to the accounts-dict key it may
        spend. Legacy accounts (plaintext key, ak_ billing keys) match by
        equality; hashed accounts match only when the RAW sk_ secret is
        presented (its sha256 prefix is the account key). A bare public
        key_id is never accepted as a credential."""
        if not presented:
            return None
        acct = self.accounts.get(presented)
        if acct is not None and not acct.get("hashed"):
            return presented
        if presented.startswith("sk_"):
            kid = creds.key_id_of(presented)
            hashed_acct = self.accounts.get(kid)
            if hashed_acct is not None and hashed_acct.get("hashed"):
                return kid
        return None

    def resolve_billing_key(self, presented: Optional[str]) -> Optional[str]:
        """Public alias of _account_key for the HTTP layer."""
        return self._account_key(presented)

    def agent_for_presented_key(self, presented: Optional[str]
                                ) -> Optional[dict[str, Any]]:
        """The agent that owns a presented credential — constant-time compare
        per record, both storage forms (plaintext / sha256), honouring
        revocation and expiry. Replaces raw equality scans."""
        if not presented:
            return None
        for a in self.agents.values():
            if creds.verify_agent_key(a, presented):
                self._note_legacy_scope_use(a)
                return a
        return None

    def _note_legacy_scope_use(self, agent: dict[str, Any]) -> None:
        """On the FIRST successful auth of a legacy-scope credential, emit a
        one-time `legacy_credential_used` audit event (key_id only, never the
        secret) and flag the record so the event fires once."""
        if not creds.is_legacy_scope(agent):
            return
        if agent.get("legacy_scope_noted"):
            return
        agent["legacy_scope_noted"] = True
        self.record_event(creds.actor_key_for_agent(agent),
                          "legacy_credential_used", agent_id=agent.get("id"),
                          key_id=agent.get("key_id"),
                          effective_scopes=list(creds.LEGACY_SCOPES))
        self._save()

    def legacy_scope_credentials(self) -> dict[str, Any]:
        """Operator view: which credentials still rely on the legacy scope
        interpretation (no explicit `scopes` field). key_ids only."""
        ids = [{"agent_id": a.get("id"), "key_id": a.get("key_id"),
                "seen": bool(a.get("legacy_scope_noted"))}
               for a in self.agents.values()
               if creds.is_legacy_scope(a) and creds.agent_has_active_key(a)]
        return {"count": len(ids), "credentials": ids}

    def get_account(self, key: str) -> Optional[dict[str, Any]]:
        resolved = self._account_key(key)
        return self.accounts.get(resolved) if resolved else None

    def record_x402_payment(self, endpoint: str, cost_credits: int,
                            settlement: dict[str, Any]) -> None:
        """Record a REAL-RAIL (x402) payment in the billing ledger and on the
        evidence chain. Sandbox credits are untouched — this is machine money
        settled through the facilitator."""
        entry = {"key": "x402", "type": "x402_payment", "endpoint": endpoint,
                 "cost_credits_equivalent": cost_credits,
                 "network": settlement.get("network"),
                 "transaction": settlement.get("transaction"),
                 "payer": settlement.get("payer"), "at": _now()}
        with self.lock, self._txn():
            self.billing_log.append(entry)
            if self.backend is not None:
                self._persist_billing(entry)
            self._save()
        self.append_ledger_event("escrow_event", {
            "event": "x402_payment", "endpoint": endpoint,
            "network": settlement.get("network"),
            "transaction": settlement.get("transaction"),
            "payer": settlement.get("payer"),
            "cost_credits_equivalent": cost_credits,
        }, actor_did="")

    def charge(self, key: str, cost: int, endpoint: str) -> dict[str, Any]:
        """Draw `cost` credits from an account. Raises UnknownAccount or
        InsufficientCredits. Returns the account."""
        with self.lock, self._txn():
            resolved = self._account_key(key)
            if resolved is None:
                raise UnknownAccount(key)
            key = resolved
            self._sync_account_from_db(key)
            acct = self.accounts.get(key)
            if acct is None:
                raise UnknownAccount(key)
            if acct["balance"] < cost:
                raise InsufficientCredits(acct["balance"], cost)
            acct["balance"] -= cost
            acct["spent"] += cost
            _entry = {
                "key": key, "type": "charge", "endpoint": endpoint,
                "amount": -cost, "balance_after": acct["balance"], "at": _now(),
            }
            self.billing_log.append(_entry)
            if self.backend is not None:
                self._persist_account(acct)
                self._persist_billing(_entry)
            self._save()
            return acct

    def credit(self, key: str, credits: int, reason: str = "topup") -> dict[str, Any]:
        with self.lock, self._txn():
            key = self._account_key(key) or key
            self._sync_account_from_db(key)
            acct = self.accounts.get(key)
            if acct is None:
                raise UnknownAccount(key)
            acct["balance"] += credits
            acct["topped_up"] += credits
            _entry = {
                "key": key, "type": reason, "amount": credits,
                "balance_after": acct["balance"], "at": _now(),
            }
            self.billing_log.append(_entry)
            if self.backend is not None:
                self._persist_account(acct)
                self._persist_billing(_entry)
            self._save()
            return acct

    def grant_trial(self, trial_credits: int, first_party: bool = False) -> dict[str, Any]:
        """Programmatic, human-free credit acquisition: an agent provisions a
        capped trial balance to evaluate the service. Play credits until real
        money is enabled — enough to run an evaluation, capped to limit abuse."""
        with self.lock, self._txn():
            acct = self._new_account(first_party=first_party)
            acct["balance"] += trial_credits
            acct["topped_up"] += trial_credits
            acct["trial"] = True
            _entry = {
                "key": acct["key"], "type": "trial_grant", "amount": trial_credits,
                "balance_after": acct["balance"], "at": _now(),
            }
            self.billing_log.append(_entry)
            if self.backend is not None:
                self._persist_account(acct)
                self._persist_billing(_entry)
            self._save()
            return acct

    # --- agent-native instrumentation ---------------------------------------
    def record_event(self, key: Optional[str], etype: str, ua: str = "", **meta) -> None:
        """Record a funnel event (query / delegation). `key` is the billing key
        (the agent's identity for instrumentation purposes). `fp` marks whether
        the actor is first-party (our own seed/test traffic) so external,
        third-party usage can be isolated."""
        key = creds.sanitize_actor_key(key)
        acct = self.accounts.get(key or "")
        fp = bool(acct and acct.get("first_party"))
        event = {"key": key or "anon", "type": etype, "ua": ua or "",
                 "fp": fp, "at": _now(), **meta}
        self.events.append(event)
        if self.backend is not None:
            self._persist_event(event)   # durable per-row (events table)
        else:
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
        if self.backend is not None:
            self._persist_agent(agent_id)
        self.record_event(self.account_for_agent(agent_id), name,
                          agent_id=agent_id,
                          agent_first_party=bool(agent.get("first_party")), **meta)
        return True

    def note_recommendations(self, key: Optional[str], worker_ids: list[str]) -> None:
        """Remember what we just recommended to `key`, so a later hire of one of
        those workers can be attributed as 'delegation following a recommendation'."""
        if not key:
            return
        acct = self.get_account(key)
        if acct is None:
            return
        recs = acct.setdefault("recent_recs", [])
        for w in worker_ids:
            recs.append(w)
        acct["recent_recs"] = recs[-50:]

    def followed_recommendation(self, key: Optional[str], worker_id: str) -> bool:
        acct = self.get_account(key or "")
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
        # Honesty hardening (2026-07-08). `genuine_external` counts any framework/
        # MCP/agent UA, so an automated poller (uptime monitor, directory crawler)
        # that hammers /a2a with bare probes and NEVER advances inflates the
        # headline. Live telemetry proved this — a single a2a:python-httpx caller
        # produced 74 of 81 genuine events over 5 days in tight identical-second
        # bursts, always a bare probe, never a capability ask, a registration, or
        # a proof.
        #
        # Two fixes make `genuine_external_engaged_detected` trustworthy:
        #   1. Per-caller actor keys. Anonymous A2A callers no longer collapse
        #      into one "a2a" bucket (see attribution.derive_a2a_actor), so one
        #      poller and one real decider are separable at actor level.
        #   2. Correct event classification (see attribution.engagement_kind).
        #      Every inbound A2A message unconditionally emits `prove_surfaced`
        #      (and, on intent, `*_howto_served`) against the caller's key —
        #      those are OUR replies, not the caller's action. The previous rule
        #      ("engaged = anything that isn't a bare probe") miscounted them as
        #      engagement, so ANY genuine poller tripped the detector. They are
        #      now classified `guild_surfacing` and excluded from both signals.
        # An actor is ENGAGED only if it emitted a `deciding` event: a capability
        # ask, a capabilities-map lookup, a prove/advert intent, a registration,
        # proof, endpoint/config declaration, delegation, attestation, or a paid
        # read. Retention is measured against genuine_external_engaged.
        from .attribution import engagement_kind, is_strong_deciding
        probe_events = [e for e in genuine if engagement_kind(e) == "probe"]
        engaged_events = [e for e in genuine if engagement_kind(e) == "deciding"]
        surfacing_events = [e for e in genuine
                            if engagement_kind(e) == "guild_surfacing"]
        engaged_actors = sorted({(e.get("key") or "anon") for e in engaged_events})
        probe_only_actors = sorted(
            {(e.get("key") or "anon") for e in probe_events} - set(engaged_actors))
        # A single capability-shaped A2A ask is `deciding`, but an automated
        # monitor could emit it, so it is necessary-but-not-sufficient proof of a
        # real returning agent. `strong` actors carry harder evidence: they
        # registered / proved / declared / delegated / attested / paid, OR issued
        # more than one deciding request. Expose both so a reader never mistakes
        # the weaker signal for the stronger one.
        deciding_by_actor: dict[str, int] = {}
        strong_actor_set: set[str] = set()
        for e in engaged_events:
            k = e.get("key") or "anon"
            deciding_by_actor[k] = deciding_by_actor.get(k, 0) + 1
            if is_strong_deciding(e):
                strong_actor_set.add(k)
        strong_actor_set |= {k for k, n in deciding_by_actor.items() if n >= 2}
        engaged_strong_actors = sorted(strong_actor_set)
        combined["genuine_external_engaged"] = self._funnel(engaged_events)
        combined["genuine_external_engaged"]["actors"] = engaged_actors
        combined["genuine_external_engaged_detected"] = bool(engaged_events)
        combined["genuine_external_engaged_strong_actors"] = engaged_strong_actors
        combined["genuine_external_engaged_strong_detected"] = bool(engaged_strong_actors)
        combined["genuine_external_probe_only_actors"] = probe_only_actors
        combined["genuine_external_probe_only_events"] = len(probe_events)
        # The count we used to MIScount as engagement — surfaced for audit.
        combined["genuine_external_guild_surfacing_events"] = len(surfacing_events)
        combined["genuine_external_engaged_note"] = (
            "genuine_external counts ANY framework/MCP/agent UA. Anonymous A2A "
            "callers are now separated at actor level (attribution.derive_a2a_actor) "
            "instead of collapsing into one 'a2a' bucket. Every inbound A2A message "
            "also emits guild-side reply events (prove_surfaced / *_howto_served) "
            "against the caller's key — these are OUR responses, counted under "
            "genuine_external_guild_surfacing_events and NEVER as engagement. "
            "genuine_external_engaged_detected is TRUE iff some genuine-external "
            "caller took at least one deciding action (not a bare probe, not a "
            "guild reply); this boolean is robust even though anonymous actor "
            "IDENTITY is a best-effort network+UA fingerprint (so per-actor COUNTS "
            "are approximate: shared NAT can merge two callers, IP rotation can "
            "split one). genuine_external_engaged_strong_actors is the "
            "higher-confidence subset (registered/proved/paid, or >1 deciding "
            "request) — a lone capability-shaped ask is deciding but an automated "
            "monitor could emit it. Measure retention against strong actors; use "
            "genuine_external_probe_only_* for the trustworthy probe-volume signal.")
        # Explicit caller-class breakdown (Pilot A instrumentation audit,
        # 2026-07-10): a closed 7-value taxonomy — AG_INTERNAL / AG_TEST /
        # REGISTRY_CRAWLER / EXTERNAL_UNKNOWN / EXTERNAL_VERIFIED /
        # EXTERNAL_MEMBER / OPERATOR — so a reader can see at a glance how much
        # traffic is crawlers or our own tests. Only EXTERNAL_* classes may
        # feed external-growth reporting (attribution.may_count_as_external_growth).
        from .attribution import caller_class
        verified_keys = {creds.actor_key_for_agent(a) for a in self.agents.values()
                         if (a.get("milestones") or {}).get("key_proof")}
        member_keys = {creds.actor_key_for_agent(a) for a in self.agents.values()}
        class_counts: dict[str, int] = {}
        for e in self.events:
            k = e.get("key")
            cls = caller_class(e, member=k in member_keys,
                               verified=k in verified_keys)
            class_counts[cls] = class_counts.get(cls, 0) + 1
        combined["caller_classes"] = class_counts
        # credential hygiene: how many active credentials still rely on the
        # legacy scope interpretation (operator-visible; key_ids only).
        combined["legacy_scope_credentials"] = self.legacy_scope_credentials()
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
        from .attribution import attribution_class
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
            # Advertised numbers must be honest: exclude our own tooling and
            # ops traffic, keep real third parties (named crawlers, frameworks,
            # anonymous externals). An agent pricing the register decision on
            # these counts must be able to trust them.
            if attribution_class(e) in ("first_party", "first_party_incident",
                                        "tooling_or_ours"):
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
        # Anonymous surfacing (2026-07-06): the A2A reply now carries the prove
        # offer, but A2A callers are usually unregistered, so no per-agent
        # `prove_offered` milestone can be stamped. Count the surfacings as
        # events instead — surfaced→started is the anonymous analogue of
        # offered→started, and without it the offer's reach is unmeasurable.
        surfaced = [e for e in self.events if e.get("type") == "prove_surfaced"]
        return {
            "external": _side(False),
            "first_party": _side(True),
            "a2a_replies_carrying_prove_offer": len(surfaced),
            "note": ("Distinct agents per stage. offered = served a guild_next "
                     "whose primary action was the proving rung; completed = "
                     "key_proof milestone (first verified proof). "
                     "a2a_replies_carrying_prove_offer counts A2A replies that "
                     "surfaced the rung to (typically anonymous) callers."),
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
            # Reachability is part of the honest answer (2026-07-10): live
            # telemetry showed an external agent asking `check: fact-check`
            # ~hourly for 3 days because /check said "hire X" about an agent
            # with no declared endpoint — a recommendation the caller cannot
            # act on. A shortlist entry must say whether the agent can
            # actually be contacted, and where.
            endpoint = a["metadata"].get("endpoint")
            # Reachability semantics: docs/discovery-swarm/REACHABILITY_SEMANTICS.md.
            # This is a READ path — it consumes the stored verification record
            # (with TTL expiry) and NEVER probes the network.
            items.append({
                "id": a["id"], "name": a["name"], "trust": round(trust, 1),
                "confidence": round(s.confidence, 2) if s else 0.0,
                "price_per_call": a["metadata"].get("price_per_call"),
                "rank": s.rank if s else 0,
                "contact": endpoint,
                **reachability_fields(endpoint, a.get("reachability")),
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
            # Demand honesty (machine-economics audit R3): before 2026-07-06 the
            # a2a first-token fallback recorded greetings ("hello", "ping") as
            # demand. Only explicit asks (marked at record time) or asks that
            # found supply count — advertised demand data must be priceable.
            if not (e.get("explicit") or e.get("supplied")):
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

    def provenance_summary(self, agent_id: str) -> dict[str, Any]:
        """Evidence-provenance leg of the AGD-1 decision contract: counts of the
        agent's ledger-committed collaborations by EFFECTIVE provenance class
        (append-only reclassifications applied), the strongest class present,
        and the checkpoint pin a verifier can anchor against. Never probes."""
        from .ledger import PROVENANCE_WEIGHT
        self.ensure_ledger_backfilled()
        # SUBSTANTIVE CHECKPOINT ANCHORING (corrective pass 2026-07-13): the
        # decision cites a checkpoint, so the evidence it counts must be the
        # evidence that checkpoint actually COMMITS. When evidence newer than
        # the last published checkpoint exists, a fresh checkpoint is published
        # FIRST (idempotent per ledger head, so publication rate is bounded by
        # real evidence writes) — a decision never cites checkpoint N while
        # counting evidence N does not commit. Records that still exceed the
        # cited checkpoint (publish failure) are excluded from counts and
        # value-tier support, and disclosed — a verifier can fetch an inclusion
        # proof (GET /ledger/inclusion/{record_id}) for every counted record
        # and recompute the path to the checkpoint's merkle root.
        published = self.latest_checkpoint(publish_if_empty=False)
        if published is None or \
           int(published.get("ledger_length", 0)) < len(self.ledger_records):
            try:
                published = self.publish_checkpoint()
            except Exception:   # publication failure -> serve committed-only
                pass
        committed_n = int(published.get("ledger_length", 0)) if published else 0
        committed = self.ledger_records[:committed_n]
        # Apply append-only reclassification entries (prov-v2 invariant): the
        # effective class, not the sealed bytes, is what evidence-weighting
        # uses. Only COMMITTED reclassifications compose — an uncommitted
        # correction must not change what the cited checkpoint vouches for.
        reclass: dict[str, str] = {}
        for d in committed:
            if d.get("type") == "reclassification":
                body = d.get("body") or d
                if body.get("target_id"):
                    reclass[body["target_id"]] = body.get("to", "")
        counts: dict[str, int] = {}
        signer_dids: set[str] = set()
        record_ids: list[str] = []
        for d in committed:
            if d.get("worker_id") != agent_id:
                continue
            prov = reclass.get(d.get("id", ""), None) or d.get("provenance")
            if not prov:
                continue
            counts[prov] = counts.get(prov, 0) + 1
            record_ids.append(d.get("id", ""))
            for s in d.get("signers") or []:
                signer_dids.add(s)
        uncommitted = sum(
            1 for d in self.ledger_records[committed_n:]
            if d.get("worker_id") == agent_id and d.get("provenance"))
        strongest = None
        for p in sorted(counts, key=lambda p: -PROVENANCE_WEIGHT.get(p, 0.0)):
            strongest = p
            break
        return {
            "counts": counts,
            "strongest": strongest,
            "verifiable_collaborations": sum(counts.values()),
            "signer_dids": sorted(signer_dids),
            "record_ids": record_ids,
            "rules_version": "prov-v2",
            "anchoring": "checkpoint_committed_only",
            "uncommitted_records_excluded": uncommitted,
            "inclusion_proof": "GET /ledger/inclusion/{record_id}",
            "checkpoint": {
                "index": published["index"] if published else None,
                "published_at": published["published_at"] if published else None,
                "head_hash": (published["checkpoint"].get("head_hash")
                              if published else None),
                "ledger_length": committed_n if published else None,
            },
        }

    @staticmethod
    def _value_at_risk_support(prov: dict[str, Any], confidence: float,
                               staleness: Optional[dict[str, Any]]) -> dict[str, Any]:
        """Which market value tiers (market.value_tier) the EVIDENCE honestly
        supports delegating at. Documented, deterministic rules — the caller
        still owns the final threshold; this is the Guild's evidence-depth
        statement, not permission."""
        counts = prov.get("counts") or {}
        total = prov.get("verifiable_collaborations", 0)
        strong = (counts.get("guild_mediated", 0)
                  + counts.get("verifiable_outcome", 0))
        days = None
        if staleness and staleness.get("age_days") is not None:
            days = staleness["age_days"]
        fresh = days is not None and days <= 30
        tiers = {
            "micro": total >= 1,
            "low": total >= 3 and confidence >= 0.2,
            "medium": strong >= 3 and confidence >= 0.4 and fresh,
            "high": counts.get("guild_mediated", 0) >= 5 and confidence >= 0.6 and fresh,
        }
        max_tier = None
        for t in ("high", "medium", "low", "micro"):
            if tiers[t]:
                max_tier = t
                break
        return {
            "tiers": tiers, "max_supported_tier": max_tier,
            "basis": ("micro: any ledger evidence; low: >=3 verifiable "
                      "collaborations, confidence>=0.2; medium: >=3 "
                      "guild_mediated/verifiable_outcome records, "
                      "confidence>=0.4, evidence<=30d; high: >=5 "
                      "guild_mediated, confidence>=0.6, evidence<=30d. "
                      "Evidence-depth statement, not permission — callers "
                      "own thresholds."),
        }

    def check(self, capability: str) -> dict[str, Any]:
        """One-call first contact: everything a brand-new agent needs to go from
        'never heard of the Guild' to a confident delegation decision *and* a
        reason to contribute back — in a single request. Collapses
        search → risk-score → proof → how-to-give-back so time-to-value is one
        call. This is the recommended entry point; the granular tools remain for
        fine-grained use."""
        short = self.shortlist(capability, limit=3)
        # `top_ranked` is the EVIDENCE-ranked #1 — informational only. The
        # provider the decision EVALUATES is bound to the provider the routing
        # gate ROUTES to (one-counterparty invariant, corrective pass
        # 2026-07-13): a machine must never approve one identity and invoke
        # another.
        top_ranked = short[0] if short else None
        # Reachability is judged across ALL suppliers, not just the top 3:
        # a reachable rank-9 agent is a better answer than an uncontactable
        # rank-1, and "no route at all" must mean no route anywhere.
        _all = self.shortlist(capability, limit=10_000)
        _reachable = [e for e in _all if e.get("has_declared_endpoint")]
        any_reachable = bool(_reachable)
        # ROUTING GATE (market loop, 2026-07-13): a provider is recommended FOR
        # ROUTING only when its endpoint is VERIFIED and currently reachable
        # (fresh recently_reachable / invocation_verified record — TTL-expired
        # or merely declared endpoints do NOT qualify) and the capability
        # matches (shortlist is capability-filtered). Otherwise `routing`
        # honestly says there is nothing to route to and what would change that.
        _routable = [e for e in _all if e.get("recommended_for_routing")]
        if _routable:
            _r = _routable[0]
            _r_rec = self.get_agent(_r["id"]) or {}
            routing = {
                "routable": True,
                "provider_id": _r["id"],
                "provider_did": _r_rec.get("did", ""),
                "name": _r["name"],
                "endpoint": _r.get("contact"),
                "endpoint_sha256": endpoint_fingerprint(_r.get("contact")),
                "trust": _r["trust"],
                "reachability_status": _r["reachability_status"],
                "verification_method": _r.get("verification_method"),
                "last_verified_at": _r.get("last_verified_at"),
                "verification_age_seconds": _r.get("verification_age_seconds"),
                "invocation_supported": _r.get("invocation_supported", False),
            }
            best = _r          # the EVALUATED provider IS the ROUTED provider
        else:
            routing = {
                "routable": False,
                "provider_id": None,
                "reason": ("no supplier of this capability has a VERIFIED, "
                           "currently reachable endpoint"
                           if any_reachable else
                           "no supplier of this capability has declared an "
                           "endpoint at all"),
                "next_step": ("suppliers: declare + verify an endpoint "
                              "(POST /agents/{id}/endpoint); buyers: register "
                              "a demand watch (POST /demand/watch)"),
            }
            best = top_ranked  # nothing routable: evaluate the evidence-top
        verdict = self.risk_for(best["id"]) if best else None
        # Demand telemetry: every /check is a demand signal for a capability.
        # Recording it (hit or miss) is what makes the be_first pitch honest —
        # a would-be supplier can see real, dated demand before registering.
        # `reachable_supply` (2026-07-10) distinguishes "supply on paper" from
        # supply a caller can actually route work to — the fact-check poller
        # taught us those are different funnels.
        self.record_event(None, "capability_demand",
                          capability=capability, supplied=bool(best),
                          reachable_supply=any_reachable,
                          explicit=True)
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
            agent_rec = self.get_agent(best["id"]) or {}
            prov = self.provenance_summary(best["id"])
            did = agent_rec.get("did", "")
            decision = {
                # AGD-1 (2026-07-13): the STABLE machine contract for the trust
                # plane. hire/caution/avoid is legacy presentation — callers own
                # thresholds; the Guild presents verifiable evidence. Fields:
                # identity, capability_match, estimate, confidence, staleness,
                # reachability, value_at_risk, evidence_provenance, policy slot.
                "contract": "AGD-1/1.0",
                "agent_id": best["id"],
                "identity": {
                    "did": did,
                    "did_method": ("did:key" if did.startswith("did:key:")
                                   else (did.split(":")[1] if did.count(":") >= 2
                                         else "unknown")),
                    "custodial": bool(agent_rec.get("custodial")),
                    # True only if this DID has cryptographically participated
                    # in ledger-committed evidence (signed receipt/attestation/
                    # offer) — never on self-assertion.
                    "did_control_proven": did in set(prov.get("signer_dids") or []),
                    "first_party": bool(agent_rec.get("first_party")),
                },
                "capability_match": {
                    "requested": capability,
                    "match": "exact",  # shortlist() is capability-filtered
                    "agent_capabilities": agent_rec.get("capabilities", []),
                },
                "estimate": verdict["estimate"],
                "confidence": verdict["confidence"],
                "staleness": verdict["staleness"],
                "value_at_risk": self._value_at_risk_support(
                    prov, verdict["confidence"], verdict["staleness"]),
                "evidence_provenance": prov,
                # Policy is the CALLER's: the Guild never decides for you. A
                # gateway/sidecar fills this slot after evaluating its owner's
                # risk policy against the fields above.
                "policy": {"result": None, "decided_by": "caller",
                           "note": "filled by the caller's own policy engine "
                                   "(e.g. the AG delegation gateway)"},
                "top_evidence": verdict["explanation"][:3],
                "interpretation": (
                    "This is an evidence estimate, not a guarantee: estimate is "
                    "the Guild's trust estimate in [0,1], confidence reflects how "
                    "much trusted evidence backs it, staleness is how old that "
                    "evidence is. You decide the threshold."
                ),
                # The minimal contract must carry reachability: an integrator
                # who reads only `decision` should never conclude "hire this
                # agent" without learning whether it can be contacted at all.
                "contact": best.get("contact"),
                # one-counterparty binding: the decision's endpoint (and its
                # fingerprint) are the endpoint work would be routed to — a
                # gateway can assert decision.endpoint_sha256 ==
                # routing.endpoint_sha256 and fail closed on any mismatch.
                "endpoint": best.get("contact"),
                "endpoint_sha256": endpoint_fingerprint(best.get("contact")),
                "has_declared_endpoint": best.get("has_declared_endpoint", False),
                "reachability_status": best.get("reachability_status", "no_endpoint"),
                "verification_method": best.get("verification_method"),
                "last_verified_at": best.get("last_verified_at"),
                "verification_age_seconds": best.get("verification_age_seconds"),
                "invocation_supported": best.get("invocation_supported", False),
                "recommended_for_routing": best.get("recommended_for_routing", False),
            }
        # ONE-COUNTERPARTY INVARIANT (fail closed): when routing says routable,
        # the decision MUST be about that exact provider — same agent id, same
        # DID, same endpoint (and fingerprint), same requested capability. A
        # violation here would let a machine approve one identity and invoke
        # another, so the routing gate closes rather than serve a mismatch.
        if routing.get("routable"):
            _bound = (
                decision is not None
                and decision.get("agent_id") == routing.get("provider_id")
                and (decision.get("identity") or {}).get("did")
                    == routing.get("provider_did")
                and decision.get("endpoint") == routing.get("endpoint")
                and decision.get("endpoint_sha256")
                    == routing.get("endpoint_sha256")
                and (decision.get("capability_match") or {}).get("requested")
                    == capability
            )
            if not _bound:
                routing = {
                    "routable": False,
                    "provider_id": None,
                    "reason": ("counterparty binding could not be established "
                               "between the decision and the routed provider — "
                               "failed closed (no route is served on a "
                               "mismatched identity)"),
                    "next_step": "retry; if this persists, report it — this "
                                 "is a Guild-side invariant violation",
                }
        out: dict[str, Any] = {
            "schema_version": 2,
            "capability": capability,
            "status": "supply" if best else "no_supply_yet",
            "routing": routing,
            "decision": decision,
            "contract_note": (
                "`decision` (AGD-1) is the stable machine contract: identity, "
                "capability match, estimate, confidence, staleness, "
                "reachability, value-at-risk support, evidence provenance, and "
                "a caller-owned policy slot. decision, routing, best_agent and "
                "verdict all concern ONE counterparty — the routed provider "
                "when routable, else the evidence-top. `highest_ranked` (when "
                "present) is informational only and never actionable. "
                "`verdict` and hire/caution/avoid are LEGACY presentation "
                "retained for v1 callers — thresholds belong to the caller, "
                "not the Guild."),
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
        # Informational-only view of the evidence-ranked #1 when it is NOT the
        # evaluated/routed provider. Deliberately shaped so a machine cannot
        # mistake it for a routable answer: no endpoint, no DID, explicit
        # actionable=False.
        if top_ranked is not None and (best is None
                                       or top_ranked["id"] != best["id"]):
            out["highest_ranked"] = {
                "agent_id": top_ranked["id"],
                "name": top_ranked["name"],
                "trust": top_ranked["trust"],
                "confidence": top_ranked["confidence"],
                "actionable": False,
                "note": ("Highest evidence-ranked supplier for this "
                         "capability. NOT the routed/evaluated provider: "
                         "`decision` and `routing` do not concern this agent. "
                         "Do not invoke, credit, or report outcomes against "
                         "it on the basis of this object."),
            }
        # Unreachable-supply honesty (2026-07-10). Live telemetry: a genuine
        # external agent asked `check: fact-check` ~29 times over 3 days. Every
        # reply said "hire Veritas-Prime" — an agent with NO declared endpoint
        # and no invoke route. The recommendation was un-actionable, so the
        # rational caller treated /check as a poll and re-asked hourly. If the
        # evidence names a best agent but nobody on the shortlist can be
        # contacted, the payload must say so, and must offer the caller a step
        # that is cheaper than polling: a registered demand watch. Honest
        # scope: the watch makes demand attributable and visible to would-be
        # suppliers NOW; outbound endpoint notification is not yet shipped, so
        # we promise re-check visibility, not callbacks.
        if best is not None and any_reachable and not best.get("has_declared_endpoint"):
            # Evidence ranks an uncontactable agent first, but a reachable
            # supplier exists further down: surface it as the actionable
            # answer rather than leaving the caller to poll.
            _br = _reachable[0]
            out["reachability"] = {
                "status": "top_ranked_no_declared_endpoint",
                "honest_answer": (
                    f"'{best['name']}' ranks first on evidence but has no "
                    "declared endpoint — the Guild cannot route work to it. "
                    f"The best supplier WITH A DECLARED ENDPOINT is "
                    f"'{_br['name']}' (trust {_br['trust']}, contact "
                    f"{_br['contact']}) — note its endpoint is declared by "
                    "the agent and unverified (reachability_status: "
                    "declared_unverified); the Guild has not checked it."
                ),
                "best_reachable": _br,
            }
        if best is not None and not any_reachable:
            out["reachability"] = {
                "status": "supply_has_no_declared_endpoint",
                "honest_answer": (
                    f"The evidence ranks '{best['name']}' first for "
                    f"'{capability}', but no agent on this shortlist has "
                    "declared an endpoint — there is currently NO route to "
                    "send work to any of them through the Guild. A "
                    "recommendation you cannot act on is not an answer, so "
                    "here is the state of the world instead."
                ),
                "stop_polling": (
                    "Re-asking this question only returns a different answer "
                    "when a provider declares an endpoint or new supply "
                    "registers. Instead of polling, register (free, one call) "
                    "and watch this capability: your demand becomes dated, "
                    "attributable signal that recruits suppliers, and your "
                    "next /check shows reachable supply the moment it exists."
                ),
                "watch_now": {
                    "register": "POST /agents/register {\"name\": \"<you>\", "
                                "\"capabilities\": [], \"metadata\": "
                                "{\"endpoint\": \"<your URL, so suppliers can "
                                "reach you>\"}} — free, returns your key",
                    "watch": "POST /demand/watch {\"capability\": \"" +
                             capability + "\"} with X-API-Key — free",
                    "then": "GET /check?capability=" + capability +
                            " on your next visit; `reachability` disappears "
                            "from this payload once a contactable provider "
                            "exists",
                },
                "if_you_supply_this": (
                    "If you (or an agent you operate) can do '" + capability +
                    "' and are reachable, this is standing demand with zero "
                    "reachable competition: register with your endpoint "
                    "declared and you are the only actionable answer."
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

    def signed_decision(self, capability: str,
                        ttl_seconds: int = 3600) -> dict[str, Any]:
        """A Guild-SIGNED AGD-1 decision: the offline-cacheable unit of the
        trust plane. Carries an eddsa-jcs-2022 DataIntegrityProof (same suite
        as passports), an explicit validity window, and the latest published
        checkpoint pin — so a delegation gateway can keep making verifiable,
        fresh-bounded decisions during a Guild outage, and any third party can
        verify the bytes without contacting the Guild."""
        from .crypto import sign_eddsa_jcs, did_key_verification_method
        res = self.check(capability)
        gid = self.guild_identity()
        published = self.latest_checkpoint(publish_if_empty=False)
        now = datetime.now(timezone.utc)
        ttl_seconds = max(60, min(int(ttl_seconds), 7 * 86400))
        unsigned: dict[str, Any] = {
            "type": "AgentGuildDecision",
            "contract": "AGD-1/1.0",
            "issuer": gid["did"],
            "capability": capability,
            "status": res["status"],
            "issued_at": now.isoformat(),
            "valid_until": (now + timedelta(seconds=ttl_seconds)).isoformat(),
            "decision": res["decision"],
            "routing": res["routing"],
            "checkpoint": {
                "index": published["index"] if published else None,
                "published_at": (published["published_at"]
                                 if published else None),
                "head_hash": (published["checkpoint"].get("head_hash")
                              if published else None),
            },
        }
        proof: dict[str, Any] = {
            "type": "DataIntegrityProof",
            "cryptosuite": "eddsa-jcs-2022",
            "created": now.isoformat(),
            "verificationMethod": did_key_verification_method(gid["did"]),
            "proofPurpose": "assertionMethod",
        }
        proof["proofValue"] = sign_eddsa_jcs(unsigned, proof, gid["private_key"])
        signed = dict(unsigned)
        signed["proof"] = proof
        return signed

    # --- one-call verifiable-collaboration recording (fills the ledger) -----
    def record_collaboration(
        self, requester: dict[str, Any], worker_id: str, capability: str,
        outcome: str, rating: float, *, deliverable: Optional[str] = None,
        deliverable_hash: Optional[str] = None, deliverable_url: Optional[str] = None,
        payment: float = 0.0, stake: float = 0.0,
        settlement: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Record a collaboration in one step: create the task, content-address
        the deliverable, submit the graded receipt, and write the requester's
        receipt-backed attestation.

        PROVENANCE TRUTH (prov-v2): this is a ONE-PARTY write path — only the
        requester authenticates. The resulting ledger entry therefore classifies
        as `mutual_attestation` (a participating agent's receipt-backed claim),
        NOT `guild_mediated`. It reaches `guild_mediated` only with independent
        proof: `settlement` (stamped internally by release_escrow — credits
        actually moved), a Guild-observed bound invocation, or the worker later
        countersigning the receipt via the two-party task flow."""
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
            # storage-exhaustion cap: deliverables are hashed, never stored, but
            # bounding the payload keeps request handling O(small)
            max_bytes = int(os.environ.get("GUILD_MAX_DELIVERABLE_BYTES", 65536))
            if len(deliverable.encode("utf-8")) > max_bytes:
                raise ValueError(f"deliverable exceeds {max_bytes} bytes; "
                                 "send deliverable_hash (sha256) instead")
            deliverable_hash = "0x" + hashlib.sha256(deliverable.encode("utf-8")).hexdigest()
        if len(str(deliverable_hash)) > 200:
            raise ValueError("deliverable_hash too long")
        task = self.create_task(requester["id"], worker_id, capability,
                                payment=float(payment))
        if settlement:
            # trusted stamp: only internal callers (release_escrow) pass this —
            # it is stripped from every client-supplied metadata dict.
            with self.lock, self._txn():
                t = self.tasks.get(task["id"])
                t.setdefault("metadata", {})["settlement"] = dict(settlement)
                if self.backend is not None:
                    self._persist_task(t)
        self.submit_receipt(task["id"], deliverable_hash, deliverable_url, outcome,
                            receipt_auth="requester")
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
        with self.lock, self._txn():
            if self.backend is not None:
                # authoritative chain: never heal against a stale in-memory list.
                self.ledger_records = self.backend.all_ledger()
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
        with self.lock, self._txn():
            from dataclasses import asdict
            from .ledger import build_record_for_task
            task = self.tasks.get(task_id)
            if task is None or not task.get("deliverable_hash"):
                return None
            if any(d.get("task_id") == task_id for d in self.ledger_records):
                return self.ledger_record_for_task(task_id)
            next_seq, head = self._ledger_head()   # authoritative under sqlite
            rec = build_record_for_task(self, task)
            rec.seq = next_seq
            rec.prev_hash = head
            rec.seal()
            d = asdict(rec)
            self.ledger_records.append(d)
            if self.backend is not None:
                self._persist_ledger(d)
            self._save()
            return d

    def append_ledger_event(self, type: str, body: dict[str, Any],
                            actor_did: str = "") -> dict[str, Any]:
        """Seal one typed event against the durable chain head and persist it
        (stage-1 dual-write: EVERY evidence-bearing mutation also lands on the
        chain; the store dicts remain the serving views until cutover).
        Bodies must contain public data only — never keys or secrets."""
        with self.lock, self._txn():
            from dataclasses import asdict
            from .ledger import GenericEntry, GENERIC_ENTRY_TYPES
            if type not in GENERIC_ENTRY_TYPES:
                raise ValueError(f"unknown ledger event type: {type}")
            next_seq, head = self._ledger_head()   # authoritative under sqlite
            e = GenericEntry(seq=next_seq, type=type, body=body,
                             actor_did=actor_did, created_at=_now(),
                             prev_hash=head).seal()
            d = asdict(e)
            self.ledger_records.append(d)
            if self.backend is not None:
                self._persist_ledger(d)
            self._save()
            return d

    def durable_ledger(self):
        """The persisted, hash-chained ledger as a verifiable Ledger object."""
        from .ledger import Ledger
        self.ensure_ledger_backfilled()
        return Ledger.from_records(self.ledger_records)

    def reclassify_ledger(self) -> dict[str, Any]:
        """prov-v2 honesty pass: re-evaluate every sealed collaboration record
        under the CURRENT classification rules and, where the sealed class is no
        longer supportable, append an append-only `reclassification` entry.
        Original bytes are never rewritten — verify_chain still passes on the
        historical records; serving views compose the correction at read time.
        Idempotent per PROVENANCE_RULES_VERSION."""
        from .ledger import (Ledger, PROVENANCE_RULES_VERSION, _classify,
                             CollaborationRecord)
        with self.lock, self._txn():
            self.ensure_ledger_backfilled()
            led = Ledger.from_records(self.ledger_records)
            done = {tid: b for tid, b in led.reclassifications().items()
                    if b.get("rule_version") == PROVENANCE_RULES_VERSION}
            appended = 0
            for rec in led.collabs():
                if rec.id in done:
                    continue
                task = self.tasks.get(rec.task_id)
                if task is not None:
                    req = self.agents.get(rec.requester_id) or {}
                    wrk = self.agents.get(rec.worker_id) or {}
                    atts = [a for a in self.attestations
                            if a.get("task_id") == rec.task_id]
                    backed = any(a.get("issuer_id") == rec.requester_id
                                 and a.get("subject_id") == rec.worker_id
                                 and a.get("verified") for a in atts)
                    to, signers, _ev = _classify(task, req, wrk, backed, atts,
                                                 task.get("metadata") or {})
                    reason = "re-evaluated under prov-v2 rules from surviving task evidence"
                else:
                    # task evidence gone: the sealed class cannot be re-derived, so
                    # fall to the strongest class ONE party's crypto supports.
                    if rec.evidence.get("attestation_ids"):
                        to, reason = "mutual_attestation", (
                            "task evidence unavailable; requester attestation is the "
                            "only surviving cryptographic participation")
                    else:
                        to, reason = "one_party_claim", (
                            "task evidence unavailable; no surviving cryptographic "
                            "participation from either party")
                if to == rec.provenance:
                    continue
                self.append_ledger_event("reclassification", {
                    "target_id": rec.id,
                    "task_id": rec.task_id,
                    "from": rec.provenance,
                    "to": to,
                    "reason": reason,
                    "rule_version": PROVENANCE_RULES_VERSION,
                }, actor_did=self.guild_did())
                appended += 1
            return {"rule_version": PROVENANCE_RULES_VERSION,
                    "examined": len(led.collabs()), "appended": appended}

    # --- published checkpoints (stage-2: pinnable canonical commitments) -----
    def publish_checkpoint(self) -> dict[str, Any]:
        """Seal the current ledger head into a Guild-signed checkpoint and add it
        to the published, append-only checkpoint feed third parties pin
        (LEDGER_ARCHITECTURE §7 stage-2). Idempotent: if no evidence has landed
        since the last published checkpoint, the existing one is returned rather
        than publishing a duplicate. Meant to be called on a schedule."""
        with self.lock, self._txn():
            if self.backend is not None:
                # build on the AUTHORITATIVE committed ledger + checkpoint feed,
                # not a possibly-stale in-memory view (concurrent appenders).
                self.ledger_records = self.backend.all_ledger()
                self.checkpoints = self.backend.all_checkpoints()
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
                # feed_version 2 (corrective pass 2026-07-13): the ENTRY itself
                # is signed (entry_proof), not only the inner checkpoint, so a
                # verifier checks feed SIGNATURES and continuity, not only
                # hashes.
                "feed_version": 2,
                # continuity: each published entry commits to its predecessor, so
                # the FEED itself is a hash chain — removing or reordering a
                # published checkpoint is detectable by anyone holding a later one.
                "prev_entry_sha256": (
                    hashlib.sha256(canonicalize(self.checkpoints[-1]).encode("utf-8"))
                    .hexdigest() if self.checkpoints else "0" * 64),
            }
            # LEGACY BRIDGE: earlier feed entries may predate predecessor
            # commitments (no prev_entry_sha256) or entry signatures. History
            # is never rewritten; instead the FIRST feed_version-2 entry after
            # such entries carries a signed, versioned bridge committing to
            # their exact bytes, so a verifier can pin legacy entries through
            # the new epoch without trusting an unauthenticated prefix.
            legacy = [e for e in self.checkpoints
                      if "prev_entry_sha256" not in e or "entry_proof" not in e]
            already_bridged = any(e.get("bridge") for e in self.checkpoints)
            if legacy and not already_bridged:
                entry["bridge"] = {
                    "version": 1,
                    "covers_indices": [e.get("index") for e in legacy],
                    "legacy_entry_sha256": [
                        hashlib.sha256(canonicalize(e).encode("utf-8")).hexdigest()
                        for e in legacy],
                    "note": ("signed epoch bridge: commits the exact bytes of "
                             "feed entries published before entry signatures/"
                             "predecessor commitments existed; legacy entries "
                             "are NOT rewritten"),
                }
            entry["entry_proof"] = sign_jcs(entry, gid["private_key"])
            self.checkpoints.append(entry)
            if self.backend is not None:
                self._persist_checkpoint(entry)
            self._save()
            return entry

    def latest_checkpoint(self, *, publish_if_empty: bool = True) -> Optional[dict[str, Any]]:
        """The most recent published checkpoint (the one passports cite). Lazily
        publishes a first one so the feed is never empty when a passport needs an
        anchor."""
        if not self.checkpoints and publish_if_empty:
            return self.publish_checkpoint()
        return self.checkpoints[-1] if self.checkpoints else None

    def ledger_inclusion_proof(self, record_id: str,
                               checkpoint_index: Optional[int] = None
                               ) -> dict[str, Any]:
        """Verifiable INCLUSION PROOF: the Merkle sibling path from one ledger
        record to the merkle_root committed by a published checkpoint. This is
        what makes checkpoint anchoring SUBSTANTIVE (corrective 2026-07-13):
        a decision that cites a checkpoint counts only evidence a third party
        can prove that checkpoint actually committed."""
        from .ledger import Ledger
        self.ensure_ledger_backfilled()
        if checkpoint_index is not None:
            if not (0 <= checkpoint_index < len(self.checkpoints)):
                raise ValueError("unknown checkpoint index")
            cp_entry = self.checkpoints[checkpoint_index]
        else:
            cp_entry = self.latest_checkpoint(publish_if_empty=False)
        if cp_entry is None:
            raise ValueError("no published checkpoint to prove against")
        length = int(cp_entry.get("ledger_length",
                                  cp_entry["checkpoint"].get("count", 0)))
        committed = self.ledger_records[:length]
        for i, d in enumerate(committed):
            if d.get("id") == record_id:
                led = Ledger.from_records(committed)
                return {
                    "record": d,
                    "seq": i,
                    "checkpoint_index": cp_entry["index"],
                    "checkpoint_merkle_root":
                        cp_entry["checkpoint"].get("merkle_root"),
                    "checkpoint_head_hash":
                        cp_entry["checkpoint"].get("head_hash"),
                    "path": led.merkle_proof(i),
                    "how_to_verify": (
                        "recompute the record's content hash (sha256 of its "
                        "canonicalised body minus hash/id), fold the sibling "
                        "path (left/right) with sha256(a+b), and compare with "
                        "checkpoint_merkle_root; verify the checkpoint's "
                        "issuer proof independently"),
                }
        raise LookupError(
            f"record {record_id} is NOT committed by checkpoint "
            f"{cp_entry['index']} (ledger_length {length}) — it is either "
            "unknown or newer than the checkpoint")

    def ledger_record(self, record_id: str) -> Optional[dict[str, Any]]:
        """Read back one sealed ledger entry by id (outcome-completion
        verification path: a write only counts once it can be read back)."""
        self.ensure_ledger_backfilled()
        for d in self.ledger_records:
            if d.get("id") == record_id:
                return d
        return None

    # --- AGO-1: requester-signed delegation outcomes -------------------------
    def record_signed_outcome(self, outcome: dict[str, Any]) -> dict[str, Any]:
        """Verify and persist a requester-SIGNED delegation outcome (AGO-1).

        Server-side contract (corrective pass 2026-07-13):
          * the outcome core is signed by the REQUESTER's registered DID —
            control of that DID is proven by the signature itself;
          * the outcome is BOUND to the gate envelope hash, the provider's id
            AND DID, the endpoint fingerprint, the task/invocation ref and the
            deliverable hash — it can never be credited to another provider;
          * the signed outcome is sealed on the append-only ledger; callers
            must read the record back (GET /ledger/record/{id}) before
            counting evidence as recorded."""
        from .crypto import verify_jcs, public_key_from_did
        required = ("type", "contract", "gate_envelope_sha256", "provider_id",
                    "provider_did", "capability", "task_ref", "outcome",
                    "reported_at", "requester_did", "proof")
        missing = [k for k in required if not outcome.get(k)]
        if missing:
            raise ValueError(f"signed outcome missing fields: {missing}")
        if outcome.get("type") != "AgentGuildOutcome" or \
           outcome.get("contract") != "AGO-1/1.0":
            raise ValueError("type must be AgentGuildOutcome, contract AGO-1/1.0")
        if outcome["outcome"] not in ("accepted", "rejected", "disputed",
                                      "blocked"):
            raise ValueError("outcome must be accepted|rejected|disputed|blocked")
        core = {k: v for k, v in outcome.items() if k != "proof"}
        requester = next((a for a in self.agents.values()
                          if a.get("did") == outcome["requester_did"]), None)
        if requester is None:
            raise ValueError("requester_did is not a registered agent — "
                             "register (optionally self-sovereign with your "
                             "own public key) before reporting outcomes")
        try:
            sig_ok = verify_jcs(core, outcome["proof"],
                                public_key_from_did(outcome["requester_did"]))
        except Exception:
            sig_ok = False
        if not sig_ok:
            raise ValueError("outcome proof does not verify against the "
                             "registered requester DID — signer does not "
                             "control that identity")
        provider = self.get_agent(outcome["provider_id"])
        if provider is None:
            raise ValueError("provider_id is not a registered agent")
        if provider.get("did", "") != outcome["provider_did"]:
            raise ValueError("provider DID mismatch: this outcome is bound to "
                             "a different identity and cannot be credited to "
                             f"agent {outcome['provider_id']}")
        entry = self.append_ledger_event(
            "signed_outcome", dict(outcome),
            actor_did=outcome["requester_did"])
        collaboration = None
        if (outcome["outcome"] in ("accepted", "rejected", "disputed")
                and requester["id"] != provider["id"]
                and outcome.get("deliverable_sha256")
                and requester.get("custodial") and requester.get("private_key")):
            # custodial requesters also get the graded-collaboration write
            # (reputation input); for self-sovereign requesters the sealed
            # signed_outcome entry itself is the evidence.
            try:
                collaboration = self.record_collaboration(
                    requester, provider["id"], outcome["capability"],
                    outcome["outcome"],
                    0.9 if outcome["outcome"] == "accepted" else 0.1,
                    deliverable_hash=str(outcome["deliverable_sha256"]))
            except (ValueError, TypeError):
                collaboration = None
        return {
            "record_id": entry["id"],
            "ledger_hash": entry["hash"],
            "seq": entry["seq"],
            "collaboration": ({"task_id": collaboration.get("task_id"),
                               "attestation_id":
                                   collaboration.get("attestation_id")}
                              if collaboration else None),
            "readback": f"/ledger/record/{entry['id']}",
        }

    # --- issuer-key rotation (continuity anchored on the chain) --------------
    def rotate_guild_identity(self) -> dict[str, Any]:
        """Rotate the Guild's issuer keypair. Continuity is a LEDGER FACT, not a
        promise: an `issuer_rotation` entry carrying the old DID, the new DID and
        signatures from BOTH keys (old endorses successor; new proves possession)
        is appended to the hash chain. Verifiers walk these entries from the
        original issuer DID to trust checkpoints signed by the current key. The
        retired PRIVATE key is dropped (public half stays derivable from its DID)."""
        with self.lock, self._txn():
            old = self.guild_identity()
            priv, pub = generate_keypair()
            rotated_at = _now()
            core = {"old_did": old["did"],
                    "new_did": did_from_public_key(pub),
                    "rotated_at": rotated_at}
            body = dict(core)
            body["proof_old_key"] = sign_jcs(core, old["private_key"])
            body["proof_new_key"] = sign_jcs(core, priv)
            entry = self.append_ledger_event("issuer_rotation", body,
                                             actor_did=old["did"])
            history = list(self.identity.get("history") or [])
            history.append({"did": old["did"], "public_key": old["public_key"],
                            "created_at": old.get("created_at"),
                            "retired_at": rotated_at})
            self.identity = {
                "did": core["new_did"], "public_key": pub, "private_key": priv,
                "name": "Agent Guild", "created_at": rotated_at,
                "history": history,
            }
            if self.backend is not None:
                self._persist_kv("identity", self.identity)
            self._save()
            return {"old_did": core["old_did"], "new_did": core["new_did"],
                    "rotated_at": rotated_at, "ledger_entry": entry}

    def guild_did_history(self) -> list[str]:
        """Every DID that has ever been the Guild issuer, oldest→current."""
        gid = self.guild_identity()
        return [h["did"] for h in (gid.get("history") or [])] + [gid["did"]]

    # --- automatic reconciliation: chain ↔ serving views ---------------------
    def reconcile_ledger(self, repair: bool = True) -> dict[str, Any]:
        """The ledger is the evidence write path; the store dicts are REPLAYABLE
        serving caches. This audit proves (or restores) that relationship:

          * chain integrity — every hash + linkage recomputed
          * completeness — every graded, content-addressed task has a sealed
            collaboration record; every agent has a register event; every
            attestation has an attestation event (repairable: missing chain
            entries are appended — append-only healing, never rewrites)
          * consistency — sealed collaboration records agree with the serving
            task on parties/outcome/deliverable (divergence is REPORTED, never
            patched: a sealed record is evidence, the serving row is cache)

        Runs at boot and on demand (GET /ledger/reconcile)."""
        with self.lock, self._txn():
            self.ensure_ledger_backfilled()
            from .ledger import Ledger
            led = Ledger.from_records(self.ledger_records)
            report: dict[str, Any] = {
                "chain_valid": led.verify_chain(),
                "records": len(self.ledger_records),
                "repaired": {"collab_records": 0, "register_events": 0,
                             "attestation_events": 0},
                "mismatches": [],
            }
            # completeness: graded tasks → collab records
            have_collab = {d.get("task_id") for d in self.ledger_records
                           if d.get("task_id") and "type" not in d}
            graded = [t for t in self.tasks.values()
                      if t.get("outcome") in ("accepted", "disputed", "rejected",
                                              "delivered")
                      and t.get("deliverable_hash")]
            for t in graded:
                if t["id"] not in have_collab:
                    if repair:
                        self.append_task_to_ledger(t["id"])
                        report["repaired"]["collab_records"] += 1
                    else:
                        report["mismatches"].append(
                            {"kind": "missing_collab_record", "task_id": t["id"]})
            # completeness: agents → register events; attestations → events
            reg_dids = {d.get("body", {}).get("did") for d in self.ledger_records
                        if d.get("type") == "register"}
            for a in self.agents.values():
                if a.get("did") and a["did"] not in reg_dids and not a.get("seed"):
                    if repair:
                        self.append_ledger_event("register", {
                            "agent_id": a["id"], "name": a.get("name", ""),
                            "did": a["did"], "capabilities": a.get("capabilities", []),
                            "custodial": a.get("custodial", True),
                            "backfilled": True,
                        }, actor_did=a["did"])
                        report["repaired"]["register_events"] += 1
                    else:
                        report["mismatches"].append(
                            {"kind": "missing_register_event", "agent_id": a["id"]})
            att_ids = {d.get("body", {}).get("attestation_id")
                       for d in self.ledger_records if d.get("type") == "attestation"}
            for att in self.attestations:
                if att["id"] not in att_ids:
                    if repair:
                        issuer_did = (self.agents.get(att.get("issuer_id")) or {}).get("did", "")
                        self.append_ledger_event("attestation", {
                            "attestation_id": att["id"],
                            "issuer_id": att.get("issuer_id"),
                            "subject_id": att.get("subject_id"),
                            "capability": att.get("capability", ""),
                            "rating": float(att.get("rating", 0.0) or 0.0),
                            "task_id": att.get("task_id"),
                            "verified": bool(att.get("verified")),
                            "credential_sha256": hashlib.sha256(canonicalize(
                                att.get("credential") or {}).encode("utf-8")).hexdigest(),
                            "backfilled": True,
                        }, actor_did=issuer_did)
                        report["repaired"]["attestation_events"] += 1
                    else:
                        report["mismatches"].append(
                            {"kind": "missing_attestation_event", "attestation_id": att["id"]})
            # consistency: sealed collab records vs serving tasks
            for d in self.ledger_records:
                if "type" in d or not d.get("task_id"):
                    continue
                t = self.tasks.get(d["task_id"])
                if t is None:
                    report["mismatches"].append(
                        {"kind": "collab_record_without_task", "record_id": d.get("id"),
                         "task_id": d["task_id"]})
                    continue
                for chain_key, task_key in (("worker_id", "worker_agent_id"),
                                            ("requester_id", "requester_agent_id"),
                                            ("outcome", "outcome"),
                                            ("deliverable_hash", "deliverable_hash")):
                    if d.get(chain_key) != t.get(task_key):
                        report["mismatches"].append(
                            {"kind": "collab_task_divergence", "record_id": d.get("id"),
                             "task_id": d["task_id"], "field": chain_key,
                             "chain": d.get(chain_key), "serving": t.get(task_key)})
            report["chain_valid_after"] = Ledger.from_records(
                self.ledger_records).verify_chain()
            report["clean"] = (report["chain_valid"] and report["chain_valid_after"]
                               and not report["mismatches"])
            return report

    # --- escrow + settlement (the economic layer) ---------------------------
    def open_escrow(self, requester_key: str, worker_id: str, amount: int,
                    capability: str = "", metadata: Optional[dict[str, Any]] = None
                    ) -> dict[str, Any]:
        """Fund an escrow: the requester locks `amount` credits for work by
        `worker_id`. Closes the trust gap — the worker can deliver knowing payment
        is held; the requester pays only on acceptance. The Guild takes a small
        settlement fee on release (its revenue on every transaction)."""
        with self.lock, self._txn():
            amount = int(amount)
            if amount <= 0:
                raise ValueError("amount must be a positive integer (credits)")
            resolved = self._account_key(requester_key)
            if resolved is None:
                raise UnknownAccount(requester_key)
            requester_key = resolved
            self._sync_account_from_db(requester_key)
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
            _hold = {"key": requester_key, "type": "escrow_hold",
                     "amount": -amount, "balance_after": acct["balance"],
                     "at": _now()}
            self.billing_log.append(_hold)
            if self.backend is not None:
                self._persist_account(acct)
                self._persist_billing(_hold)
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
            if self.backend is not None:
                self._persist_escrow(esc)
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
        may release — this public path requires the RAW requester credential."""
        with self.lock, self._txn():
            self._sync_escrow_from_db(escrow_id)   # authoritative status/amount
            esc = self.escrows.get(escrow_id)
            if esc is None:
                raise ValueError("escrow not found")
            requester_key = self._account_key(requester_key)
            if not requester_key or esc["requester_key"] != requester_key:
                raise ValueError("only the funding party may release this escrow")
            return self.release_escrow_internal(
                escrow_id, reason="requester_release",
                deliverable=deliverable, deliverable_hash=deliverable_hash,
                rating=rating)

    def release_escrow_internal(self, escrow_id: str, *, reason: str,
                                deliverable: Optional[str] = None,
                                deliverable_hash: Optional[str] = None,
                                rating: float = 1.0,
                                expected_status: tuple = ("funded",)
                                ) -> dict[str, Any]:
        """INTERNAL settlement operation (corrective pass 2026-07-13).

        Authorization comes from the escrow's own immutable ownership + state
        (status == funded), never from replaying a stored credential: under
        GUILD_HASH_KEYS=1 the store holds only public key ids, so deterministic
        timeouts and dispute execution — rules both parties accepted when the
        offer was signed — CANNOT and MUST NOT present a raw secret. This
        method is never routed from HTTP; public callers go through
        release_escrow(), which requires the raw requester credential.
        `reason` records WHICH deterministic rule settled (audit trail)."""
        with self.lock, self._txn():
            self._sync_escrow_from_db(escrow_id)   # authoritative status/amount
            esc = self.escrows.get(escrow_id)
            if esc is None:
                raise ValueError("escrow not found")
            if esc["status"] not in expected_status:
                raise ValueError(f"escrow is {esc['status']}, "
                                 f"not in {expected_status}")
            esc.setdefault("metadata", {})["settlement_reason"] = reason
            amount, fee = esc["amount"], esc["fee"]
            payout = amount - fee
            worker_key = self.account_for_agent(esc["worker_id"])
            if worker_key:
                self.credit(worker_key, payout, reason="escrow_payout")
            esc["status"] = "released"
            esc["settled_at"] = _now()
            if self.backend is not None:
                # Settle the escrow FIRST (status=released is now committed-in-txn
                # on this connection), then DERIVE guild_revenue as the SUM of
                # fees over released escrows — never a read-modify-write counter.
                # Because the sum is keyed by escrow_id (each escrow settles
                # exactly once, guarded by the status=funded check above),
                # concurrent releases can neither clobber nor double-count it.
                self._persist_escrow(esc)
                self.guild_revenue = self.backend.guild_revenue_total()
            else:
                self.guild_revenue += fee
            _fee = {"key": "guild", "type": "settlement_fee",
                    "amount": fee, "balance_after": self.guild_revenue,
                    "at": _now(), "escrow_id": escrow_id}
            self.billing_log.append(_fee)
            if self.backend is not None:
                self._persist_billing(_fee)
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
                        payment=float(amount),
                        # independent settlement proof: credits actually moved
                        # through Guild escrow — the internally-stamped evidence
                        # that lets this record classify as guild_mediated.
                        settlement={"escrow_id": escrow_id, "amount": amount,
                                    "fee": fee, "settled_at": esc["settled_at"]})
                    esc["task_id"] = res.get("task_id")
                except ValueError:
                    pass
            if self.backend is not None:
                self._persist_escrow(esc)
            self._save()
            self.append_ledger_event("escrow_event", {
                "event": "released", "escrow_id": escrow_id,
                "requester_id": esc["requester_id"], "worker_id": esc["worker_id"],
                "capability": esc.get("capability", ""), "amount": amount,
                "fee": fee, "payout": payout, "task_id": esc["task_id"],
                "reason": reason,
            }, actor_did=(self.agents.get(esc["requester_id"]) or {}).get("did", ""))
            if self.backend is not None:
                # authoritative refresh of the derived-revenue cache after the
                # settlement + ledger rows are sealed (idempotent by escrow_id).
                self.guild_revenue = self.backend.guild_revenue_total()
            _vcr = self.ledger_record_for_task(esc["task_id"]) if esc.get("task_id") else None
            return {"escrow_id": escrow_id, "status": "released", "amount": amount,
                    "fee": fee, "payout": payout, "worker_id": esc["worker_id"],
                    "guild_revenue": self.guild_revenue, "task_id": esc["task_id"],
                    "collaboration": {"provenance": (_vcr or {}).get("provenance"),
                                      "record_id": (_vcr or {}).get("id"),
                                      "signers": (_vcr or {}).get("signers")}}

    def refund_escrow(self, escrow_id: str, requester_key: str) -> dict[str, Any]:
        """Cancel and refund a funded escrow back to the requester (no fee, since no
        value was exchanged). Only the payer may refund, and only before release —
        this public path requires the RAW requester credential."""
        with self.lock, self._txn():
            self._sync_escrow_from_db(escrow_id)   # authoritative status
            esc = self.escrows.get(escrow_id)
            if esc is None:
                raise ValueError("escrow not found")
            requester_key = self._account_key(requester_key)
            if not requester_key or esc["requester_key"] != requester_key:
                raise ValueError("only the funding party may refund this escrow")
            return self.refund_escrow_internal(escrow_id,
                                               reason="requester_refund")

    def refund_escrow_internal(self, escrow_id: str, *, reason: str,
                               expected_status: tuple = ("funded",)
                               ) -> dict[str, Any]:
        """INTERNAL refund operation — authorization by escrow ownership/state
        (status == funded), never by replaying a credential (see
        release_escrow_internal). The refund is credited to the escrow's own
        immutable `requester_key` account key; no secret is required or used."""
        with self.lock, self._txn():
            self._sync_escrow_from_db(escrow_id)   # authoritative status
            esc = self.escrows.get(escrow_id)
            if esc is None:
                raise ValueError("escrow not found")
            if esc["status"] not in expected_status:
                raise ValueError(f"escrow is {esc['status']}, "
                                 f"not in {expected_status}")
            self.credit(esc["requester_key"], esc["amount"],
                        reason="escrow_refund")
            esc["status"] = "refunded"
            esc["settled_at"] = _now()
            esc.setdefault("metadata", {})["settlement_reason"] = reason
            if self.backend is not None:
                self._persist_escrow(esc)
            self._save()
            self.append_ledger_event("escrow_event", {
                "event": "refunded", "escrow_id": escrow_id,
                "requester_id": esc["requester_id"], "worker_id": esc["worker_id"],
                "amount": esc["amount"], "reason": reason,
            }, actor_did=(self.agents.get(esc["requester_id"]) or {}).get("did", ""))
            return {"escrow_id": escrow_id, "status": "refunded",
                    "amount": esc["amount"], "reason": reason}

    def reverse_settlement_internal(self, escrow_id: str, *,
                                    reason: str) -> dict[str, Any]:
        """Reverse a settled escrow back into the DISPUTED state for an appeal
        round (corrective pass 2026-07-13 — the previous appeal path re-armed
        an already-settled escrow and would have paid twice).

        released -> disputed: the worker's payout is clawed back and the
        Guild's fee is surrendered; refunded -> disputed: the requester's
        refund is re-held. If the funds are no longer available (payout
        already spent), the reversal FAILS and the appeal is rejected — funds
        never go negative and never move twice."""
        with self.lock, self._txn():
            self._sync_escrow_from_db(escrow_id)
            esc = self.escrows.get(escrow_id)
            if esc is None:
                raise ValueError("escrow not found")
            amount, fee = esc["amount"], esc["fee"]
            if esc["status"] == "released":
                worker_key = self.account_for_agent(esc["worker_id"])
                acct = self.accounts.get(worker_key) if worker_key else None
                payout = amount - fee
                if acct is None or acct["balance"] < payout:
                    raise ValueError(
                        "cannot reverse settlement: the payout is no longer "
                        "available in the worker's account — appeal rejected, "
                        "round-1 verdict stands")
                acct["balance"] -= payout
                _entry = {"key": worker_key, "type": "settlement_reversal",
                          "amount": -payout, "balance_after": acct["balance"],
                          "at": _now(), "escrow_id": escrow_id}
                self.billing_log.append(_entry)
                if self.backend is not None:
                    self._persist_account(acct)
                    self._persist_billing(_entry)
                if self.backend is None:
                    self.guild_revenue -= fee
            elif esc["status"] == "refunded":
                racct = self.accounts.get(esc["requester_key"])
                if racct is None or racct["balance"] < amount:
                    raise ValueError(
                        "cannot reverse refund: funds no longer available in "
                        "the requester's account — appeal rejected")
                racct["balance"] -= amount
                _entry = {"key": esc["requester_key"],
                          "type": "settlement_reversal", "amount": -amount,
                          "balance_after": racct["balance"], "at": _now(),
                          "escrow_id": escrow_id}
                self.billing_log.append(_entry)
                if self.backend is not None:
                    self._persist_account(racct)
                    self._persist_billing(_entry)
            else:
                raise ValueError(f"escrow is {esc['status']}: only a settled "
                                 "escrow can be reversed for appeal")
            prior = esc["status"]
            esc["status"] = "disputed"
            esc["settled_at"] = None
            esc.setdefault("metadata", {})["reversed_from"] = prior
            if self.backend is not None:
                self._persist_escrow(esc)
                self.guild_revenue = self.backend.guild_revenue_total()
            self._save()
            self.append_ledger_event("escrow_event", {
                "event": "settlement_reversed", "escrow_id": escrow_id,
                "from_status": prior, "reason": reason,
                "amount": amount, "fee": fee,
            }, actor_did="")
            # the round-1 release wrote a guild_mediated (independent
            # settlement) collaboration; that basis is now void. History is
            # never rewritten — an append-only reclassification downgrades it.
            if prior == "released" and esc.get("task_id"):
                rec = self.ledger_record_for_task(esc["task_id"])
                if rec and rec.get("provenance") == "guild_mediated":
                    from .ledger import PROVENANCE_RULES_VERSION
                    self.append_ledger_event("reclassification", {
                        "target_id": rec["id"], "task_id": esc["task_id"],
                        "from": "guild_mediated", "to": "one_party_claim",
                        "reason": f"settlement reversed ({reason})",
                        "rule_version": PROVENANCE_RULES_VERSION,
                    }, actor_did=self.guild_did())
            return {"escrow_id": escrow_id, "status": "disputed",
                    "reversed_from": prior}

    def dispute_escrow(self, escrow_id: str, actor_key: str, grounds: str = ""
                       ) -> dict[str, Any]:
        """Flag a funded escrow as disputed; funds stay held pending resolution.
        Either party (payer or worker) may raise it."""
        with self.lock, self._txn():
            self._sync_escrow_from_db(escrow_id)   # authoritative status
            esc = self.escrows.get(escrow_id)
            if esc is None:
                raise ValueError("escrow not found")
            actor_key = self._account_key(actor_key)
            actor = self.accounts.get(actor_key) if actor_key else None
            actor_agent = actor.get("owner_agent_id") if actor else None
            if (not actor_key or esc["requester_key"] != actor_key) \
                    and actor_agent != esc["worker_id"]:
                raise ValueError("only a party to this escrow may dispute it")
            if esc["status"] != "funded":
                raise ValueError(f"escrow is {esc['status']}, not funded")
            esc["status"] = "disputed"
            esc["dispute"] = {"by": actor_agent or actor_key, "grounds": grounds, "at": _now()}
            if self.backend is not None:
                self._persist_escrow(esc)
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
        """The economic dashboard — HONEST reporting classes (corrective pass
        2026-07-13). All escrow settlement is in SANDBOX CREDITS: sandbox
        credits are never presented as USD revenue. Actual revenue is zero
        until an independently verifiable on-chain/fiat settlement exists.

        Classes:
          first_party_sandbox         — at least one party is Guild-operated
          third_party_unconsented     — no Guild-operated party, but the
                                        provider never accepted signed terms /
                                        received a payout (e.g. Guild-observed
                                        invocation of a public endpoint)
          consenting_external_sandbox — no Guild-operated party AND the
                                        provider accepted the offer/terms and
                                        received the sandbox payout
          real_settlement             — on-chain/fiat settlements (x402);
                                        counted ONLY from recorded facilitator
                                        transactions, none exist until the
                                        treasury is funded."""
        def _first_party(esc: dict) -> bool:
            req = self.agents.get(esc.get("requester_id")) or {}
            wrk = self.agents.get(esc.get("worker_id")) or {}
            return bool(req.get("first_party") or wrk.get("first_party"))

        def _provider_consented(esc: dict) -> bool:
            # consent = the worker itself accepted the signed offer (two-party
            # acceptance) AND an account of the worker's received the payout.
            offer_id = (esc.get("metadata") or {}).get("offer_id")
            offer = (self.__dict__.get("offers") or {}).get(offer_id or "")
            accepted = bool(offer and offer.get("accept"))
            paid_out = bool(self.account_for_agent(esc.get("worker_id") or ""))
            return accepted and paid_out

        released = [e for e in self.escrows.values() if e["status"] == "released"]
        classes: dict[str, list] = {"first_party_sandbox": [],
                                    "third_party_unconsented": [],
                                    "consenting_external_sandbox": []}
        for e in released:
            if _first_party(e):
                classes["first_party_sandbox"].append(e)
            elif _provider_consented(e):
                classes["consenting_external_sandbox"].append(e)
            else:
                classes["third_party_unconsented"].append(e)
        by_status: dict[str, int] = {}
        for e in self.escrows.values():
            by_status[e["status"]] = by_status.get(e["status"], 0) + 1
        x402_payments = [b for b in self.billing_log
                         if b.get("type") == "x402_payment"]
        out: dict[str, Any] = {
            "currency": "credits_sandbox",
            "honesty": ("sandbox credits are NOT money; nothing here is USD "
                        "revenue. real_settlement counts only independently "
                        "verifiable on-chain/fiat transactions."),
            "fee_bps": settlement_fee_bps(),
            "escrows": len(self.escrows),
            "by_status": by_status,
            "settled_count": len(released),
            "settled_volume_credits": sum(e["amount"] for e in released),
            "guild_fee_credits": sum(e["fee"] for e in released),
            "unresolved_settlement_failures": len(
                getattr(self, "settlement_failures", {}) or {}),
        }
        for name, rows in classes.items():
            out[name] = {
                "settled_count": len(rows),
                "settled_volume_credits": sum(e["amount"] for e in rows),
                "guild_fee_credits": sum(e["fee"] for e in rows),
            }
        out["real_settlement"] = {
            "transactions": len(x402_payments),
            "revenue_usd": 0.0 if not x402_payments else None,
            "note": ("x402 rail is READY BUT INACTIVE: no funded treasury, "
                     "so no real settlement exists and actual revenue is "
                     "zero" if not x402_payments else
                     "verify each transaction independently on its network"),
        }
        return out

    # --- the Guild's own signing identity + portable passports --------------
    def guild_identity(self) -> dict[str, Any]:
        """The Guild's persistent ed25519 signing identity. Created once and
        persisted, so the Guild can issue credentials (Agent Passports) in its own
        name that anyone can verify offline against this did:key. This is the
        issuer-of-record position — the credit-bureau anchor for agent reputation."""
        with self.lock, self._txn():
            if not self.identity:
                priv, pub = generate_keypair()
                self.identity = {
                    "did": did_from_public_key(pub),
                    "public_key": pub,
                    "private_key": priv,
                    "name": "Agent Guild",
                    "created_at": _now(),
                }
                if self.backend is not None:
                    self._persist_kv("identity", self.identity)
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
        # a credential issued by ANY historical Guild key is Guild-issued; the
        # rotation chain on the ledger proves the succession (issuer_rotation)
        is_guild = bool(issuer) and issuer in self.guild_did_history()
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
        with self.lock, self._txn():
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
                if self.backend is not None:
                    self._persist_health(snap)
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
        with self.lock, self._txn():
            task_id = "task_" + secrets.token_hex(6)
            # Provenance-truth guard: these metadata keys are EVIDENCE STAMPS the
            # store writes internally (worker-authenticated receipt, settled
            # escrow, Guild-observed invocation). A client must never be able to
            # supply them and elevate its own record's provenance class.
            meta = dict(metadata or {})
            for k in TRUSTED_TASK_META_KEYS:
                meta.pop(k, None)
            rec = {
                "id": task_id,
                "requester_agent_id": requester_id,
                "worker_agent_id": worker_id,
                "task_type": task_type,
                "payment": float(payment),     # simulated cost/payment
                "metadata": meta,
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
            if self.backend is not None:
                self._persist_task(rec)
            # dual-write: task creation is chain evidence too — with this, every
            # evidence-bearing mutation (register, config_change, task_created,
            # receipt, attestation, escrow_event) lands on the chain, making the
            # serving views REPLAYABLE caches (see reconcile_ledger).
            req_did = (self.agents.get(requester_id) or {}).get("did", "")
            self.append_ledger_event("task_created", {
                "task_id": task_id,
                "requester_id": requester_id,
                "worker_id": worker_id,
                "task_type": task_type,
                "payment": float(payment),
                "worker_config_hash": rec["worker_config_hash"],
                "requester_config_hash": rec["requester_config_hash"],
            }, actor_did=req_did)
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
        receipt_auth: str = "unauthenticated",
    ) -> dict[str, Any]:
        """Record a task receipt. `receipt_auth` is the trusted evidence stamp of
        WHO cryptographically stood behind this receipt — decided by the caller
        from actual authentication, never from client-supplied data:
          worker_key        — the worker authenticated with its own credential
          worker_signature  — a signature verified against the worker's DID
          requester         — the requester recorded it on the worker's behalf
          unauthenticated   — nobody proved anything (classifies lowest)"""
        if receipt_auth not in ("worker_key", "worker_signature",
                                "requester", "guild_observed", "unauthenticated"):
            raise ValueError("invalid receipt_auth")
        with self.lock, self._txn():
            self._sync_task_from_db(task_id)       # authoritative current task
            task = self.tasks.get(task_id)
            if not task:
                raise ValueError("task not found")
            task["deliverable_hash"] = deliverable_hash
            task["deliverable_url"] = deliverable_url
            task["outcome"] = outcome
            task["delivered_at"] = _now()
            task.setdefault("metadata", {})["receipt_auth"] = receipt_auth
            if outcome != "rejected":
                # First delivered work = first-time activation (worker side).
                self.record_milestone(task["worker_agent_id"], "first_receipt",
                                      task_id=task_id)
            if self.backend is not None:
                self._persist_task(task)
            self._rep_cache = None
            self._save()
        # NOTE: a submitted receipt does NOT upgrade reachability. A receipt
        # does not prove AG invoked the agent's declared endpoint (work may have
        # gone through another channel, or reference an unknown invocation).
        # invocation_verified comes ONLY from the trusted AG-originated
        # begin/complete_outbound_invocation flow.
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
                "receipt_auth": receipt_auth,
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
        with self.lock, self._txn():
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
        with self.lock, self._txn():
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
        if self.backend is not None:
            self._persist_attestation(rec)
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
