"""Retry-idempotency proof for the sqlite backend (``GUILD_STORE=sqlite``).

The backend retries on SQLITE_BUSY / lock conflict (per-connection
``busy_timeout`` + a bounded exponential backoff in ``SqliteBackend._retry``).
This suite PROVES that a bounded retry NEVER duplicates a committed side effect
— the invariant is: *a retried transaction commits EXACTLY ONCE or fails
explicitly; never partially, never twice.*

Two things make that true, and both are exercised here:

1. The retry boundary is a SINGLE SQL statement — ``BEGIN IMMEDIATE`` (in
   ``_begin``), a ``put_*``/append (in ``_exec``), or ``COMMIT`` (in
   ``_commit``) — never a replay of the Python method body. SQLite guarantees a
   statement that returns SQLITE_BUSY did NOT modify the database (the lock is
   refused before any page is written), so re-issuing it cannot double-apply.
   The fault injector below models exactly that: it raises
   ``OperationalError("database is locked")`` *instead of* running the statement
   (i.e. BUSY-before-apply), once or twice, before letting it through.

2. Every effect is idempotent by a natural key on replay: agents.id,
   accounts.key, tasks.id, escrows.id, invocations.id, ledger.seq (INSERT OR
   REPLACE), rotation keyed on the single agent row, and guild_revenue is a
   DERIVED SUM over settled escrows (never an incremented counter).

We assert NO duplication for each of the seven side-effect types called out in
the amendment: events, ledger records, billing entries, receipts, escrow
movements, revenue increments, and credential-rotation events. A companion
multi-PROCESS proof under real contention lives in ``test_sqlite_backend.py``
(tests 16 + 18).
"""
import contextlib
import os
import sqlite3

import pytest


@pytest.fixture()
def store(monkeypatch, tmp_path):
    db = str(tmp_path / "idem.sqlite3")
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    monkeypatch.setenv("GUILD_STORE_PATH", db)
    monkeypatch.setenv("GUILD_DATA", "")
    from app.store import Store
    s = Store(path="")
    s._dbfile = db
    return s


class _ConProxy:
    """Delegates to the real sqlite connection, but calls ``fault(op, sql)``
    first so a test can raise SQLITE_BUSY *before* the real statement runs
    (modelling BUSY-before-apply — the only way real SQLite raises it)."""
    def __init__(self, real, fault):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_fault", fault)

    def execute(self, sql, *a, **k):
        self._fault("execute", sql)
        return self._real.execute(sql, *a, **k)

    def commit(self, *a, **k):
        self._fault("commit", None)
        return self._real.commit(*a, **k)

    def rollback(self, *a, **k):
        return self._real.rollback(*a, **k)

    def __getattr__(self, name):
        return getattr(self._real, name)


@contextlib.contextmanager
def inject(store, kind, times):
    """Install a BUSY fault on the store's connection: ``kind='begin'`` fails the
    next ``times`` ``BEGIN IMMEDIATE`` statements; ``kind='commit'`` fails the
    next ``times`` commits. Each failure is raised BEFORE the real op runs."""
    b = store.backend
    real = b.conn()
    state = {"n": 0}

    def fault(op, sql):
        hit = ((kind == "begin" and op == "execute" and sql
                and "BEGIN IMMEDIATE" in sql)
               or (kind == "commit" and op == "commit"))
        if hit and state["n"] < times:
            state["n"] += 1
            raise sqlite3.OperationalError("database is locked")

    b._local.con = _ConProxy(real, fault)
    try:
        yield state
    finally:
        b._local.con = real


def _db(store):
    return sqlite3.connect(store._dbfile)


def _count(store, sql, params=()):
    con = _db(store)
    try:
        return con.execute(sql, params).fetchone()[0]
    finally:
        con.close()


# --- sanity: the injector actually drives the retry path ---------------------
def test_injector_exercises_the_retry(store):
    with inject(store, "begin", 2) as st:
        store.record_event("k", "probe_retry", i=0)
    assert st["n"] == 2, "the BEGIN fault did not fire the expected number of times"


# --- 1. events: no duplicate row under BEGIN-busy and COMMIT-busy ------------
def test_events_not_duplicated(store):
    with inject(store, "begin", 2):
        store.record_event("actor", "ev_once", i=7)
    assert _count(store, "SELECT COUNT(*) FROM events WHERE type='ev_once'") == 1
    with inject(store, "commit", 1):
        store.record_event("actor", "ev_commit", i=8)
    assert _count(store, "SELECT COUNT(*) FROM events WHERE type='ev_commit'") == 1
    # in-memory list matches the DB (one, not two)
    assert sum(1 for e in store.events if e["type"] == "ev_once") == 1
    assert sum(1 for e in store.events if e["type"] == "ev_commit") == 1


# --- 2. ledger records: exactly one new row, seq stays contiguous ------------
def test_ledger_append_not_duplicated(store):
    before = _count(store, "SELECT COUNT(*) FROM ledger")
    with inject(store, "begin", 2):
        store.append_ledger_event("config_change", {"tag": "idem", "i": 1})
    with inject(store, "commit", 1):
        store.append_ledger_event("config_change", {"tag": "idem", "i": 2})
    con = _db(store)
    seqs = [r[0] for r in con.execute("SELECT seq FROM ledger ORDER BY seq")]
    con.close()
    assert len(seqs) == before + 2, (
        f"expected exactly {before + 2} ledger rows, observed {len(seqs)}")
    assert seqs == list(range(before + 2)), f"ledger seq not contiguous: {seqs}"
    assert store.durable_ledger().verify_chain() is True


# --- 3. billing entries: one row per credit/charge, no double -----------------
def test_billing_entries_not_duplicated(store):
    agent = store.register_agent("biller", ["cap"], {})
    key = agent["api_key"]
    start = store.get_account(key)["balance"]
    with inject(store, "begin", 2):
        store.credit(key, 500, reason="seed_once")
    with inject(store, "commit", 1):
        store.charge(key, 40, "read")
    seed = _count(store, "SELECT COUNT(*) FROM billing_log "
                  "WHERE json_extract(json,'$.type')='seed_once'")
    charges = _count(store, "SELECT COUNT(*) FROM billing_log "
                     "WHERE json_extract(json,'$.type')='charge'")
    assert seed == 1, f"expected 1 seed_once billing row, observed {seed}"
    assert charges == 1, f"expected 1 charge billing row, observed {charges}"
    # balance reflects exactly one credit and one charge
    assert store.get_account(key)["balance"] == start + 500 - 40


# --- 4. receipts: task settled once, one receipt ledger row ------------------
def test_receipt_not_duplicated(store):
    req = store.register_agent("rq", ["cap"], {})
    wrk = store.register_agent("wk", ["cap"], {})
    tid = store.create_task(req["id"], wrk["id"], "cap")["id"]
    with inject(store, "begin", 2):
        store.submit_receipt(tid, "0x" + "aa" * 32, outcome="accepted")
    assert store.tasks[tid]["outcome"] == "accepted"
    receipts = _count(store, "SELECT COUNT(*) FROM ledger "
                      "WHERE json_extract(json,'$.type')='receipt' "
                      "AND json_extract(json,'$.body.task_id')=?", (tid,))
    assert receipts == 1, f"expected exactly 1 receipt ledger row, observed {receipts}"
    first_receipt_ms = _count(store, "SELECT COUNT(*) FROM events "
                              "WHERE type='first_receipt'")
    assert first_receipt_ms == 1, (
        f"expected exactly 1 first_receipt milestone event, observed {first_receipt_ms}")


# --- 5. escrow movements: one escrow, one hold, one release ------------------
def test_escrow_movements_not_duplicated(store):
    payer = store.register_agent("epay", ["cap"], {})
    worker = store.register_agent("ework", ["cap"], {})
    key = payer["api_key"]
    store.credit(key, 10_000, reason="seed")
    with inject(store, "begin", 2):
        esc = store.open_escrow(key, worker["id"], 100, capability="c")
    eid = esc["id"]
    assert _count(store, "SELECT COUNT(*) FROM escrows WHERE id=?", (eid,)) == 1
    holds = _count(store, "SELECT COUNT(*) FROM billing_log "
                   "WHERE json_extract(json,'$.type')='escrow_hold'")
    assert holds == 1, f"expected exactly 1 escrow_hold, observed {holds}"
    with inject(store, "commit", 1):
        store.release_escrow(eid, key, deliverable_hash="0x" + "bb" * 32)
    released = _count(store, "SELECT COUNT(*) FROM escrows "
                      "WHERE id=? AND status='released'", (eid,))
    assert released == 1, f"expected the escrow released exactly once, got {released}"
    payouts = _count(store, "SELECT COUNT(*) FROM billing_log "
                     "WHERE json_extract(json,'$.type')='escrow_payout'")
    assert payouts == 1, f"expected exactly 1 worker payout, observed {payouts}"


# --- 6. revenue increments: derived fee counted exactly once -----------------
def test_revenue_increment_not_duplicated(store):
    payer = store.register_agent("rpay", ["cap"], {})
    worker = store.register_agent("rwork", ["cap"], {})
    key = payer["api_key"]
    store.credit(key, 10_000, reason="seed")
    esc = store.open_escrow(key, worker["id"], 200, capability="c")
    fee = esc["fee"]
    with inject(store, "begin", 2):
        out = store.release_escrow(esc["id"], key, deliverable_hash="0x" + "cc" * 32)
    # revenue is a DERIVED SUM: exactly one fee, counted once, even through the
    # retried BEGIN. One settlement_fee billing row, and cache == query.
    fee_rows = _count(store, "SELECT COUNT(*) FROM billing_log "
                      "WHERE json_extract(json,'$.type')='settlement_fee'")
    assert fee_rows == 1, f"expected exactly 1 settlement_fee row, observed {fee_rows}"
    assert out["guild_revenue"] == fee
    assert store.guild_revenue == fee
    assert store.backend.guild_revenue_total() == fee
    # a reopened store re-derives the same value (no double-count on reload)
    from app.store import Store
    s2 = Store(path="")
    assert s2.guild_revenue == fee


# --- 7. credential-rotation events: one active key, one rotation event --------
def test_rotation_not_duplicated(store):
    from app import credentials as creds
    agent = store.register_agent("rotor", ["cap"], {})
    aid = agent["id"]
    with inject(store, "begin", 2):
        store.rotate_api_key(aid)
    with inject(store, "commit", 1):
        store.rotate_api_key(aid)
    # exactly one agent row, carrying exactly one active credential
    assert _count(store, "SELECT COUNT(*) FROM agents WHERE id=?", (aid,)) == 1
    rec = store.agents[aid]
    assert creds.agent_has_active_key(rec), "agent lost its credential after rotation"
    # exactly two rotation events (one per successful rotate) — not four
    rotations = _count(store, "SELECT COUNT(*) FROM events WHERE type='api_key_rotated'")
    assert rotations == 2, (
        f"expected exactly 2 api_key_rotated events (2 rotations), observed "
        f"{rotations} — a retried rotation double-emitted its event")
    # exactly one account row for the agent (no orphan rekey rows on retry)
    active = creds.actor_key_for_agent(rec)
    keys = [r[0] for r in _db(store).execute(
        "SELECT key FROM accounts WHERE owner_agent_id=?", (aid,)).fetchall()]
    assert keys == [active], f"orphan account rows after retried rotation: {keys}"
    assert store.backend.integrity_check() == "ok"
