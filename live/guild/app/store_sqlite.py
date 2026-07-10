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
    json         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agents_key_id ON agents (key_id);

CREATE TABLE IF NOT EXISTS accounts (
    key            TEXT PRIMARY KEY,
    owner_agent_id TEXT,
    balance        INTEGER,
    json           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS accounts_owner ON accounts (owner_agent_id);

CREATE TABLE IF NOT EXISTS tasks (
    id   TEXT PRIMARY KEY,
    json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attestations (
    id   TEXT PRIMARY KEY,
    json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS escrows (
    id            TEXT PRIMARY KEY,
    status        TEXT,
    requester_key TEXT,
    json          TEXT NOT NULL
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

-- singletons + transient registers: identity, swarm_state, guild_revenue,
-- outbound_invocations. Whole-value blobs keyed by name.
CREATE TABLE IF NOT EXISTS kv (
    k    TEXT PRIMARY KEY,
    json TEXT NOT NULL
);
"""

# collections persisted as whole-value blobs in kv.
KV_SINGLETONS = ("identity", "swarm_state", "guild_revenue", "outbound_invocations")


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
        row = con.execute("SELECT json FROM kv WHERE k='_schema_version'").fetchone()
        if row is None:
            con.execute("INSERT OR REPLACE INTO kv VALUES ('_schema_version', ?)",
                        (_j(SCHEMA_VERSION),))

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
        residual contention (e.g. an upgrade deadlock) rather than surfacing it."""
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
    def put_agent(self, rec: dict[str, Any]) -> None:
        self._exec(
            "INSERT OR REPLACE INTO agents (id,key_id,api_key_hash,revoked_at,expires_at,json)"
            " VALUES (?,?,?,?,?,?)",
            (rec.get("id"), rec.get("key_id"), rec.get("api_key_hash"),
             rec.get("api_key_revoked_at"), rec.get("api_key_expires_at"), _j(rec)))

    def put_account(self, rec: dict[str, Any]) -> None:
        self._exec(
            "INSERT OR REPLACE INTO accounts (key,owner_agent_id,balance,json) VALUES (?,?,?,?)",
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
        self._exec("INSERT OR REPLACE INTO tasks (id,json) VALUES (?,?)",
                   (rec.get("id"), _j(rec)))

    def put_attestation(self, rec: dict[str, Any]) -> None:
        self._exec("INSERT OR REPLACE INTO attestations (id,json) VALUES (?,?)",
                   (rec.get("id"), _j(rec)))

    def put_escrow(self, rec: dict[str, Any]) -> None:
        self._exec(
            "INSERT OR REPLACE INTO escrows (id,status,requester_key,json) VALUES (?,?,?,?)",
            (rec.get("id"), rec.get("status"), rec.get("requester_key"), _j(rec)))

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
                  "ledger", "events"):
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
            "guild_revenue": kv.get("guild_revenue", 0),
            "outbound_invocations": kv.get("outbound_invocations", {}),
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
