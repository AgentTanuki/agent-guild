"""SqliteBackend — a per-entity, write-through, crash-safe persistence backend
for the Guild Store (opt-in behind ``GUILD_STORE=sqlite``; the default JSON
store is untouched, byte for byte).

Why this exists
---------------
The JSON store persists by atomically ``os.replace()``-ing the WHOLE state file
on every mutation. That is correct for a single writer, but under concurrent
writers the last replace clobbers everything the other writer committed: the
measured failure is ~50% lost writes with two processes registering agents
(docs/discovery-swarm/harness/results/concurrency_results.txt). This backend
stores ONE ROW PER ENTITY, so two processes writing *different* entities both
land, and two processes writing the *same* entity serialize (BEGIN IMMEDIATE +
busy_timeout) with a last-writer-wins outcome instead of mutual destruction.

Configuration (all REQUIRED; each has a reason):
  * ``PRAGMA journal_mode=WAL``       — writers don't block readers and a reader
                                        never sees a half-written transaction;
                                        the WAL survives process crash and is
                                        replayed on next open (durability).
  * ``PRAGMA foreign_keys=ON``        — referential integrity is enforced by the
                                        engine. We keep declared FKs soft to
                                        preserve the JSON store's tolerance of
                                        dangling refs, but the pragma is on so
                                        any FK we do declare is honoured.
  * ``PRAGMA busy_timeout=5000``      — on SQLITE_BUSY, block up to 5s for the
                                        lock instead of erroring immediately;
                                        this is what makes same-entity contention
                                        serialize rather than fail.
  * ``PRAGMA synchronous=NORMAL``     — in WAL mode this is the durable+fast
                                        sweet spot: safe against application and
                                        OS crash; a power loss can only lose the
                                        very last (uncheckpointed) transactions,
                                        never corrupt the DB.
  * per-thread connections            — SQLite connection objects are NOT safe to
                                        share across threads; each thread gets its
                                        own via ``threading.local`` rather than
                                        one shared connection.
  * explicit ``BEGIN IMMEDIATE``      — write transactions take the reserved lock
                                        up front, so multi-statement invariants
                                        (registration+account+credential, etc.)
                                        are all-or-nothing and cross-process
                                        writers queue deterministically.
  * retry-on-SQLITE_BUSY              — belt and braces on top of busy_timeout:
                                        a bounded exponential backoff around the
                                        transaction body.
  * ``PRAGMA wal_autocheckpoint=1000``— bound WAL growth; the WAL is folded back
                                        into the main file automatically.
  * ``PRAGMA integrity_check``        — exposed via ``integrity_check()`` for the
                                        restart/recovery tests and ops.

Schema — ONE ROW PER ENTITY (never one row for the whole document). Every table
keeps the FULL original record in a ``json`` column so nothing is ever lost;
a few columns are lifted out and indexed for the hot lookups (auth by key_id,
billing by account key, escrow by status/requester).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable, Optional

# Bumped when the on-disk schema changes in a non-additive way.
SCHEMA_VERSION = 1

BUSY_TIMEOUT_MS = 5000
WAL_AUTOCHECKPOINT = 1000
# retry-on-SQLITE_BUSY: bounded exponential backoff on top of busy_timeout.
_MAX_RETRIES = 6
_BASE_BACKOFF_S = 0.05

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id           TEXT PRIMARY KEY,
    key_id       TEXT,
    api_key_hash TEXT,
    revoked_at   TEXT,
    expires_at   TEXT,
    json         TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS agents_key_id ON agents (key_id);

CREATE TABLE IF NOT EXISTS accounts (
    key            TEXT PRIMARY KEY,
    owner_agent_id TEXT,
    balance        INTEGER,
    json           TEXT NOT NULL,
    version        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS accounts_owner ON accounts (owner_agent_id);

CREATE TABLE IF NOT EXISTS tasks (
    id      TEXT PRIMARY KEY,
    json    TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS attestations (
    id   TEXT PRIMARY KEY,
    json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS escrows (
    id            TEXT PRIMARY KEY,
    status        TEXT,
    requester_key TEXT,
    json          TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS escrows_requester ON escrows (requester_key);

-- seq is the ledger record's own monotonic sequence (NOT autoincrement); it
-- preserves the hash-chain order exactly.
CREATE TABLE IF NOT EXISTS ledger (
    seq  INTEGER PRIMARY KEY,
    json TEXT NOT NULL
);

-- seq AUTOINCREMENT preserves append order across concurrent writers even when
-- two events share the same wall-clock `at`. Pure ordered append (no dedup
-- index): the live JSON store keeps duplicate (at,type,key) rows in memory, so
-- the runtime backend does too; crash atomicity is handled by transactions.
CREATE TABLE IF NOT EXISTS events (
    seq  INTEGER PRIMARY KEY AUTOINCREMENT,
    at   TEXT,
    type TEXT,
    key  TEXT,
    json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_type_at ON events (type, at);

-- one edge per referred agent (referred_id is the natural key) so the
-- activation update is an in-place upsert, not an append.
CREATE TABLE IF NOT EXISTS referrals (
    referred_id TEXT PRIMARY KEY,
    json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoints (
    idx  INTEGER PRIMARY KEY,
    json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS billing_log (
    seq  INTEGER PRIMARY KEY AUTOINCREMENT,
    json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS health_log (
    seq  INTEGER PRIMARY KEY AUTOINCREMENT,
    json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS demand_watches (
    seq  INTEGER PRIMARY KEY AUTOINCREMENT,
    json TEXT NOT NULL
);

-- Trusted AG-originated outbound invocations get a DEDICATED table (not a kv
-- blob) so begin binds (id, agent_id, fingerprint, status=open) atomically and
-- complete does an authoritative single-row read inside the same transaction.
CREATE TABLE IF NOT EXISTS outbound_invocations (
    id                   TEXT PRIMARY KEY,
    agent_id             TEXT,
    endpoint_fingerprint TEXT,
    status               TEXT,
    started_at           TEXT,
    completed_at         TEXT,
    expires_at           TEXT,
    json                 TEXT NOT NULL,
    version              INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS oinv_agent ON outbound_invocations (agent_id);

-- singletons + transient registers: identity, swarm_state, guild_revenue.
-- Whole-value blobs keyed by name. (outbound_invocations is now its own table.)
CREATE TABLE IF NOT EXISTS kv (
    k    TEXT PRIMARY KEY,
    json TEXT NOT NULL
);
"""

# collections persisted as whole-value blobs in kv. ``guild_revenue`` is kept as
# a denormalised CACHE only — the authoritative value is DERIVED via
# ``guild_revenue_total()`` (SUM of fees over released escrows), never read back
# from kv as a mutable counter (see SQLITE_SCHEMA.md, amendment 1).
KV_SINGLETONS = ("identity", "swarm_state", "guild_revenue")


def _j(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


class SqliteBackend:
    """Per-thread-connection, WAL-mode, per-entity persistence.

    All *writes* go through :meth:`transaction` (BEGIN IMMEDIATE ... COMMIT) so a
    crash can never commit half of a multi-entity invariant. The Store opens one
    ``transaction()`` around a whole mutating method; the per-entity ``put_*``
    hooks executed inside it all land atomically.
    """

    def __init__(self, path: str):
        if not path:
            raise ValueError("SqliteBackend requires a file path")
        self.path = path
        # A shared-cache in-memory URI (file:...mode=memory&cache=shared) mirrors
        # the JSON store's "path='' -> ephemeral, private" behavior: each Store
        # gets its own in-process database, shared across its threads.
        self._uri = path.startswith("file:")
        if not self._uri:
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self._local = threading.local()
        # An in-memory shared-cache DB only lives while a connection is open; the
        # backend keeps this bootstrap connection for its whole lifetime.
        self._keepalive = self.conn()
        self._init_schema()

    # --- connection management ---------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000.0,
                              isolation_level=None,   # autocommit; we do BEGIN by hand
                              uri=self._uri)
        # busy_timeout MUST be set before the WAL switch: on a brand-new DB,
        # concurrent openers race to flip journal_mode=WAL (a write) and without
        # a busy timeout that race raises SQLITE_BUSY immediately. Retry the
        # switch too, belt and braces, until one opener wins and the rest see WAL.
        con.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        delay = _BASE_BACKOFF_S
        for _ in range(_MAX_RETRIES + 4):
            try:
                con.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc):
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 1.0)
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute(f"PRAGMA wal_autocheckpoint={WAL_AUTOCHECKPOINT}")
        return con

    def conn(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = self._connect()
            self._local.con = con
            self._local.depth = 0
        return con

    def _init_schema(self) -> None:
        con = self.conn()
        con.executescript(SCHEMA)
        # Additive on-disk migration: a DB created by an earlier build of this
        # backend may lack the `version` columns (optimistic-concurrency) — add
        # them in place. CREATE TABLE IF NOT EXISTS already handles the new
        # outbound_invocations table. Purely additive, so it is safe to re-run.
        for table in ("agents", "accounts", "tasks", "escrows"):
            self._ensure_column(con, table, "version", "INTEGER NOT NULL DEFAULT 0")
        row = con.execute("SELECT json FROM kv WHERE k='_schema_version'").fetchone()
        if row is None:
            con.execute("INSERT OR REPLACE INTO kv VALUES ('_schema_version', ?)",
                        (_j(SCHEMA_VERSION),))

    @staticmethod
    def _ensure_column(con: sqlite3.Connection, table: str, col: str, decl: str) -> None:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    # --- transactions (re-entrant, BEGIN IMMEDIATE, retry-on-BUSY) ----------
    def _begin(self) -> None:
        con = self.conn()
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            self._retry(lambda: con.execute("BEGIN IMMEDIATE"))
        self._local.depth = depth + 1

    def _commit(self) -> None:
        con = self.conn()
        depth = getattr(self._local, "depth", 1) - 1
        self._local.depth = depth
        if depth == 0:
            self._retry(con.commit)

    def _rollback(self) -> None:
        con = self.conn()
        self._local.depth = 0
        try:
            con.rollback()
        except sqlite3.Error:
            pass

    def _retry(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` with a bounded exponential backoff on SQLITE_BUSY. The
        per-connection busy_timeout already blocks up to 5s; this catches the
        residual contention (e.g. an upgrade deadlock) rather than surfacing it.

        RETRY IDEMPOTENCY (why a retry can NEVER duplicate a committed effect):
        the retry boundary is a SINGLE SQL statement (``BEGIN IMMEDIATE`` in
        ``_begin``, a ``put_*``/append INSERT/UPDATE in ``_exec``, or ``COMMIT``
        in ``_commit``) — never a re-run of the Python method body, so no
        in-Python mutation is ever replayed. SQLite guarantees a statement that
        returns ``SQLITE_BUSY`` did NOT modify the database (the lock is refused
        BEFORE any page is written), so re-issuing a BUSY'd statement cannot
        double-apply it. ``BEGIN IMMEDIATE`` only ever fails BUSY while acquiring
        the write lock (nothing applied yet); once held, subsequent writes in the
        same transaction do not contend, and ``COMMIT`` is atomic (a BUSY'd
        commit left the transaction open — the retry commits it exactly once; a
        commit that already succeeded closes the transaction, so a spurious retry
        is a harmless no-op). On top of that, every write is idempotent by a
        natural key (agents.id, accounts.key, tasks.id, escrows.id,
        outbound_invocations.id, ledger.seq via INSERT OR REPLACE), and
        guild_revenue is DERIVED (a SUM over settled escrows), never an
        incremented counter — so even a whole-unit replay re-derives the same
        state instead of duplicating it. Proven by
        ``tests/test_sqlite_retry_idempotency.py``."""
        delay = _BASE_BACKOFF_S
        last: Optional[Exception] = None
        for _ in range(_MAX_RETRIES):
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc):
                    raise
                last = exc
                time.sleep(delay)
                delay = min(delay * 2, 1.0)
        if last is not None:
            raise last

    class _Txn:
        def __init__(self, backend: "SqliteBackend"):
            self._b = backend

        def __enter__(self):
            self._b._begin()
            return self._b

        def __exit__(self, exc_type, exc, tb):
            if exc_type is not None:
                self._b._rollback()
                return False
            self._b._commit()
            return False

    def transaction(self) -> "SqliteBackend._Txn":
        """Re-entrant write transaction. Outermost = BEGIN IMMEDIATE / COMMIT;
        nested = joins the enclosing transaction."""
        return SqliteBackend._Txn(self)

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> None:
        self._retry(lambda: self.conn().execute(sql, params))

    def in_transaction(self) -> bool:
        """True if this thread currently has an open BEGIN IMMEDIATE (used by
        Store._save to no-op inside a wrapped mutating method)."""
        return getattr(self._local, "depth", 0) > 0


    # --- per-entity write-through hooks ------------------------------------
    # The write-sensitive tables (agents, accounts, tasks, escrows,
    # outbound_invocations) carry an integer `version` column. Every ``put_*``
    # here is an UPSERT that BUMPS ``version`` on update (optimistic-concurrency
    # stamp), so an authoritative-read-then-write done inside one BEGIN
    # IMMEDIATE transaction can never silently clobber newer state, and a
    # compare-and-swap (``*_cas``) is available for callers that want to detect
    # the conflict explicitly rather than serialize on the write lock.
    def put_agent(self, rec: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO agents (id,key_id,api_key_hash,revoked_at,expires_at,json,version)"
            " VALUES (?,?,?,?,?,?,0)"
            " ON CONFLICT(id) DO UPDATE SET key_id=excluded.key_id,"
            " api_key_hash=excluded.api_key_hash, revoked_at=excluded.revoked_at,"
            " expires_at=excluded.expires_at, json=excluded.json,"
            " version=agents.version+1",
            (rec.get("id"), rec.get("key_id"), rec.get("api_key_hash"),
             rec.get("api_key_revoked_at"), rec.get("api_key_expires_at"), _j(rec)))

    def update_agent_cas(self, rec: dict[str, Any], expected_version: int) -> bool:
        """Compare-and-swap an agent row: apply ONLY if the stored version is
        still ``expected_version``, bumping it on success. Returns True if the
        write landed, False if a newer version was already committed (a stale
        write — the caller must re-read and retry, never silently overwrite)."""
        cur = self._retry(lambda: self.conn().execute(
            "UPDATE agents SET key_id=?,api_key_hash=?,revoked_at=?,expires_at=?,"
            "json=?,version=version+1 WHERE id=? AND version=?",
            (rec.get("key_id"), rec.get("api_key_hash"),
             rec.get("api_key_revoked_at"), rec.get("api_key_expires_at"),
             _j(rec), rec.get("id"), expected_version)))
        return cur.rowcount == 1

    def fetch_agent(self, agent_id: str):
        """Authoritative agent record straight from the DB (read INSIDE a write
        transaction so credential/endpoint mutations validate against the latest
        committed row, not a stale in-memory snapshot)."""
        row = self.conn().execute(
            "SELECT json,version FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not row:
            return None
        rec = json.loads(row[0])
        rec["_version"] = row[1]
        return rec

    def put_account(self, rec: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO accounts (key,owner_agent_id,balance,json,version)"
            " VALUES (?,?,?,?,0)"
            " ON CONFLICT(key) DO UPDATE SET owner_agent_id=excluded.owner_agent_id,"
            " balance=excluded.balance, json=excluded.json, version=accounts.version+1",
            (rec.get("key"), rec.get("owner_agent_id"), rec.get("balance"), _j(rec)))

    def delete_account(self, key: str) -> None:
        self._exec("DELETE FROM accounts WHERE key=?", (key,))

    def fetch_account(self, key: str):
        """Authoritative account record straight from the DB. Money mutations
        call this INSIDE their BEGIN IMMEDIATE transaction so the read-modify-
        write runs against the latest committed balance, not a stale in-memory
        copy — the single point where concurrent debits could otherwise double
        spend (the reserved write lock guarantees no writer interleaves)."""
        row = self.conn().execute(
            "SELECT json FROM accounts WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_task(self, rec: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO tasks (id,json,version) VALUES (?,?,0)"
            " ON CONFLICT(id) DO UPDATE SET json=excluded.json, version=tasks.version+1",
            (rec.get("id"), _j(rec)))

    def fetch_task(self, task_id: str):
        """Authoritative task record straight from the DB (used by receipt
        acceptance + task-state transitions inside their write transaction)."""
        row = self.conn().execute(
            "SELECT json FROM tasks WHERE id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_attestation(self, rec: dict[str, Any]) -> None:
        self._exec("INSERT OR REPLACE INTO attestations (id,json) VALUES (?,?)",
                   (rec.get("id"), _j(rec)))

    def put_escrow(self, rec: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO escrows (id,status,requester_key,json,version)"
            " VALUES (?,?,?,?,0)"
            " ON CONFLICT(id) DO UPDATE SET status=excluded.status,"
            " requester_key=excluded.requester_key, json=excluded.json,"
            " version=escrows.version+1",
            (rec.get("id"), rec.get("status"), rec.get("requester_key"), _j(rec)))

    def fetch_escrow(self, escrow_id: str):
        """Authoritative escrow record straight from the DB. Escrow
        release/refund/dispute read THIS (not the in-memory copy) inside their
        BEGIN IMMEDIATE transaction, so a concurrent release cannot settle the
        same escrow twice and guild_revenue cannot be clobbered."""
        row = self.conn().execute(
            "SELECT json FROM escrows WHERE id=?", (escrow_id,)).fetchone()
        return json.loads(row[0]) if row else None

    # --- outbound invocations (dedicated table) ----------------------------
    def put_invocation(self, rec: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO outbound_invocations"
            " (id,agent_id,endpoint_fingerprint,status,started_at,completed_at,expires_at,json,version)"
            " VALUES (?,?,?,?,?,?,?,?,0)"
            " ON CONFLICT(id) DO UPDATE SET agent_id=excluded.agent_id,"
            " endpoint_fingerprint=excluded.endpoint_fingerprint, status=excluded.status,"
            " started_at=excluded.started_at, completed_at=excluded.completed_at,"
            " expires_at=excluded.expires_at, json=excluded.json,"
            " version=outbound_invocations.version+1",
            (rec.get("id") or rec.get("invocation_id"), rec.get("agent_id"),
             rec.get("endpoint_fingerprint"), rec.get("status"),
             rec.get("started_at") or rec.get("created_at"), rec.get("completed_at"),
             rec.get("expires_at"), _j(rec)))

    def fetch_invocation(self, invocation_id: str):
        """Authoritative invocation row straight from the DB (complete reads it
        inside the write transaction to verify status=open + fingerprint)."""
        row = self.conn().execute(
            "SELECT json FROM outbound_invocations WHERE id=?", (invocation_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def fetch_kv(self, name: str, default: Any = None):
        """Authoritative singleton value straight from the DB (e.g. the
        guild_revenue counter, read inside an escrow-release transaction so
        concurrent releases accumulate the fee instead of clobbering it)."""
        row = self.conn().execute(
            "SELECT json FROM kv WHERE k=?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def guild_revenue_total(self) -> int:
        """Guild revenue DERIVED from committed settlement records, NEVER a
        mutable read-modify-write counter: the SUM of the settlement ``fee`` over
        every escrow that is committed in ``status='released'``. Each escrow
        settles EXACTLY ONCE (release is guarded by the authoritative
        ``status='funded'`` read on the escrow row), so this sum is idempotent by
        ``escrow_id`` (the primary key) — concurrent releases can neither clobber
        nor double-count it, and it is ALWAYS exact. Called inside the release
        transaction (this release's row is already visible on the same
        connection) and on load, to refresh the in-memory ``guild_revenue``
        cache. See docs/discovery-swarm/SQLITE_SCHEMA.md for why the escrows
        table (not the ledger) is the derivation source."""
        row = self.conn().execute(
            "SELECT COALESCE(SUM(json_extract(json,'$.fee')),0) FROM escrows "
            "WHERE status='released'").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def ledger_settlement_fee_total(self) -> int:
        """The SAME revenue figure DERIVED FROM THE LEDGER: SUM of ``body.fee``
        over every ``escrow_event`` ledger record. Each released escrow seals
        exactly one ``escrow_event`` carrying its ``fee`` (keyed by escrow_id,
        guarded by the ``status='funded'`` read), so every settlement fee is
        represented in the ledger unambiguously — once. This reconciles with
        ``guild_revenue_total()`` (the authoritative escrows-derived value); a
        divergence would mean a settlement fee is missing from or duplicated in
        the ledger."""
        # Only a RELEASED escrow_event is a settlement. opened events also carry
        # the (prospective) fee and refunded events carry none — so the sum is
        # taken over body.event='released' to match settled revenue exactly.
        row = self.conn().execute(
            "SELECT COALESCE(SUM(json_extract(json,'$.body.fee')),0) FROM ledger "
            "WHERE json_extract(json,'$.type')='escrow_event' "
            "AND json_extract(json,'$.body.event')='released'").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def fetch_ledger_head(self) -> tuple:
        """(next_seq, prev_hash) computed AUTHORITATIVELY from the committed
        ledger rows, so concurrent appenders each seal against the true chain
        head and produce a contiguous, gap-free `seq` instead of clobbering."""
        row = self.conn().execute(
            "SELECT seq,json FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
        if not row:
            return 0, ("0" * 64)
        return int(row[0]) + 1, json.loads(row[1]).get("hash", "0" * 64)

    def all_ledger(self) -> list:
        return [json.loads(r[0]) for r in
                self.conn().execute("SELECT json FROM ledger ORDER BY seq").fetchall()]

    def all_checkpoints(self) -> list:
        return [json.loads(r[0]) for r in
                self.conn().execute("SELECT json FROM checkpoints ORDER BY idx").fetchall()]

    def put_ledger(self, rec: dict[str, Any]) -> None:
        self._exec("INSERT OR REPLACE INTO ledger (seq,json) VALUES (?,?)",
                   (rec.get("seq"), _j(rec)))

    def append_event(self, ev: dict[str, Any]) -> None:
        # Pure ordered append (AUTOINCREMENT seq). Mirrors the JSON store's
        # in-memory events list, which keeps duplicate (at,type,key) rows; crash
        # atomicity comes from transactions, so no dedup index is needed here
        # (the migration EXPORT applies the journal dedup rule separately).
        self._exec("INSERT INTO events (at,type,key,json) VALUES (?,?,?,?)",
                   (ev.get("at"), ev.get("type"), ev.get("key"), _j(ev)))

    def put_referral(self, rec: dict[str, Any]) -> None:
        self._exec("INSERT OR REPLACE INTO referrals (referred_id,json) VALUES (?,?)",
                   (rec.get("referred_id"), _j(rec)))

    def put_checkpoint(self, rec: dict[str, Any]) -> None:
        self._exec("INSERT OR REPLACE INTO checkpoints (idx,json) VALUES (?,?)",
                   (rec.get("index"), _j(rec)))

    def append_billing(self, rec: dict[str, Any]) -> None:
        self._exec("INSERT INTO billing_log (json) VALUES (?)", (_j(rec),))

    def append_health(self, rec: dict[str, Any]) -> None:
        self._exec("INSERT INTO health_log (json) VALUES (?)", (_j(rec),))

    def append_demand_watch(self, rec: dict[str, Any]) -> None:
        self._exec("INSERT INTO demand_watches (json) VALUES (?)", (_j(rec),))

    def put_kv(self, name: str, value: Any) -> None:
        self._exec("INSERT OR REPLACE INTO kv (k,json) VALUES (?,?)", (name, _j(value)))

    # --- bulk load (reads stay in-memory in the Store; this hydrates it) ----
    def is_empty(self) -> bool:
        con = self.conn()
        for t in ("agents", "accounts", "tasks", "attestations", "escrows",
                  "ledger", "events", "outbound_invocations"):
            if con.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone():
                return False
        # kv singletons alone (e.g. identity) also count as non-empty.
        row = con.execute(
            "SELECT 1 FROM kv WHERE k NOT LIKE '\\_%' ESCAPE '\\' LIMIT 1").fetchone()
        return row is None

    def load_all(self) -> dict[str, Any]:
        con = self.conn()

        def rows(sql: str) -> list[dict[str, Any]]:
            return [json.loads(r[0]) for r in con.execute(sql).fetchall()]

        agents = {r["id"]: r for r in rows("SELECT json FROM agents")}
        accounts = {r["key"]: r for r in rows("SELECT json FROM accounts")}
        tasks = {r["id"]: r for r in rows("SELECT json FROM tasks")}
        escrows = {r["id"]: r for r in rows("SELECT json FROM escrows")}
        attestations = rows("SELECT json FROM attestations ORDER BY rowid")
        ledger = rows("SELECT json FROM ledger ORDER BY seq")
        events = rows("SELECT json FROM events ORDER BY seq")
        referrals = rows("SELECT json FROM referrals ORDER BY rowid")
        checkpoints = rows("SELECT json FROM checkpoints ORDER BY idx")
        billing_log = rows("SELECT json FROM billing_log ORDER BY seq")
        health_log = rows("SELECT json FROM health_log ORDER BY seq")
        demand_watches = rows("SELECT json FROM demand_watches ORDER BY seq")
        # outbound_invocations now has its OWN table (keyed by invocation id).
        invocations = {r["id"]: r for r in
                       rows("SELECT json FROM outbound_invocations ORDER BY started_at")}
        kv = {r[0]: json.loads(r[1]) for r in
              con.execute("SELECT k,json FROM kv").fetchall()}
        return {
            "agents": agents, "accounts": accounts, "tasks": tasks,
            "escrows": escrows, "attestations": attestations, "ledger": ledger,
            "events": events, "referrals": referrals, "checkpoints": checkpoints,
            "billing_log": billing_log, "health_log": health_log,
            "demand_watches": demand_watches,
            "identity": kv.get("identity", {}),
            "swarm_state": kv.get("swarm_state", {}),
            # DERIVED (idempotent by escrow_id), not the vestigial kv counter.
            "guild_revenue": self.guild_revenue_total(),
            "outbound_invocations": invocations,
        }

    # --- ops helpers --------------------------------------------------------
    def integrity_check(self) -> str:
        row = self.conn().execute("PRAGMA integrity_check").fetchone()
        return row[0] if row else "unknown"

    def wal_checkpoint(self, mode: str = "PASSIVE") -> tuple:
        row = self.conn().execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        return tuple(row) if row else (0, 0, 0)

    def close(self) -> None:
        con = getattr(self._local, "con", None)
        if con is not None:
            try:
                con.close()
            finally:
                self._local.con = None
                self._local.depth = 0
