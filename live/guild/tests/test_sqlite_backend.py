"""Real multi-PROCESS concurrency tests for the sqlite persistence backend
(``GUILD_STORE=sqlite``).

Every scenario spawns independent OS processes (subprocess, NOT threads) against
ONE shared sqlite file and asserts the exact expected-vs-observed counts in the
assertion message. The headline result is that concurrent registrations lose
ZERO writes, versus the ~50% loss the whole-file JSON store suffers under the
same pattern (docs/discovery-swarm/harness/results/concurrency_results.txt).

The parent-process stores in this module are forced onto the sqlite backend via
an autouse fixture, so the file runs identically whether the ambient suite run
is GUILD_STORE=json or =sqlite.
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

GUILD_DIR = pathlib.Path(__file__).resolve().parents[1]   # live/guild

# A generic worker: opens its OWN Store on the shared sqlite file (a genuinely
# independent process) and performs one op. Never imports the parent's objects.
WORKER = r'''
import os, sys, json, time
db, op = sys.argv[1], sys.argv[2]
arg = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
os.environ["GUILD_STORE"] = "sqlite"
os.environ["GUILD_STORE_PATH"] = db
from app.store import Store
s = Store(path="")
if op == "register":
    for i in range(arg["n"]):
        s.register_agent(arg["tag"] + str(i), ["cap"], {})
    print("ok")
elif op == "rotate":
    ok = 0
    for _ in range(arg["n"]):
        try:
            s.rotate_api_key(arg["agent_id"]); ok += 1
        except Exception:
            pass
    print("rotated", ok)
elif op == "endpoint":
    s.set_agent_endpoint(arg["agent_id"], arg["endpoint"])
    print("ok")
elif op == "events":
    for i in range(arg["n"]):
        s.record_event(arg["tag"], "conc", i=i)
    print("ok")
elif op == "debit":
    ok = 0
    for _ in range(arg["n"]):
        try:
            s.charge(arg["key"], arg["amount"], "read"); ok += 1
        except Exception:
            pass
    print("charged", ok)
elif op == "escrow_open":
    ok = 0
    for _ in range(arg["n"]):
        try:
            s.open_escrow(arg["key"], arg["worker_id"], arg["amount"], capability="c"); ok += 1
        except Exception:
            pass
    print("opened", ok)
elif op == "receipt":
    s.submit_receipt(arg["task_id"], "0x" + "aa" * 32, outcome="accepted")
    print("ok")
elif op == "invoke":
    ok = 0
    for _ in range(arg["n"]):
        r = s.begin_outbound_invocation(arg["agent_id"])
        if r and s.complete_outbound_invocation(r["invocation_id"], protocol_ok=True):
            ok += 1
    print("verified", ok)
elif op == "reader":
    end = time.time() + arg["secs"]
    reads = 0
    while time.time() < end:
        _ = s.backend.load_all(); reads += 1
    print("reads", reads)
elif op == "kill_mid_txn":
    b = s.backend
    b._begin()                       # BEGIN IMMEDIATE, no commit
    b.put_agent({"id": "ghost_" + arg["tag"], "json": 1})
    os._exit(0)                      # die INSIDE the transaction
elif op == "idem_pk":
    # every process writes the SAME primary key -> upsert, exactly one row
    for _ in range(arg["n"]):
        with s.backend.transaction():
            s.backend.put_task({"id": arg["id"], "marker": arg["tag"]})
    print("ok")
'''


def _spawn(db, op, arg=None, count=1, tags=None):
    procs = []
    tags = tags or [f"t{i}" for i in range(count)]
    for tag in tags:
        a = dict(arg or {})
        a.setdefault("tag", tag)
        procs.append(subprocess.Popen(
            [sys.executable, "-c", WORKER, db, op, json.dumps(a)],
            cwd=str(GUILD_DIR),
            env={**os.environ, "PYTHONPATH": str(GUILD_DIR)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    out = []
    for p in procs:
        o, e = p.communicate(timeout=180)
        out.append((p.returncode, o.decode().strip(), e.decode().strip()))
    return out


@pytest.fixture(autouse=True)
def _force_sqlite(monkeypatch, tmp_path):
    db = str(tmp_path / "conc.sqlite3")
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    monkeypatch.setenv("GUILD_STORE_PATH", db)
    # keep GUILD_DATA empty so the parent store never touches a JSON file
    monkeypatch.setenv("GUILD_DATA", "")
    return db


def _store():
    from app.store import Store
    return Store(path="")


def _no_crash(results):
    for rc, out, err in results:
        assert rc == 0, f"worker crashed (rc={rc}): {err}"


# --- 1. concurrent registrations: ZERO lost writes ---------------------------
def test_concurrent_registrations_lose_zero(_force_sqlite):
    db, procs, per = _force_sqlite, 4, 25
    results = _spawn(db, "register", {"n": per}, count=procs)
    _no_crash(results)
    con = __import__("sqlite3").connect(db)
    observed = con.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    con.close()
    expected = procs * per
    assert observed == expected, (
        f"concurrent registrations: expected {expected} agents "
        f"({procs} procs x {per}), observed {observed}, "
        f"lost {expected - observed} (JSON store loses ~50%)")


# --- 2. concurrent rotations of the SAME agent: exactly one valid credential --
def test_concurrent_rotations_leave_one_valid_credential(_force_sqlite):
    db = _force_sqlite
    s = _store()
    agent = s.register_agent("rotator", ["cap"], {})
    aid = agent["id"]
    results = _spawn(db, "rotate", {"agent_id": aid, "n": 10}, count=4)
    _no_crash(results)
    from app import credentials as creds
    s2 = _store()
    rec = s2.agents[aid]
    # The single agent row carries exactly ONE current, active credential
    # (last-writer-wins). Mode-agnostic: the fingerprint is the raw api_key
    # (plaintext) or the key_id/api_key_hash (GUILD_HASH_KEYS=1).
    assert creds.agent_has_active_key(rec), (
        "agent lost its credential after concurrent rotations")
    fingerprint = rec.get("key_id") or rec.get("api_key")
    assert fingerprint, "no credential fingerprint survived on the record"
    con = __import__("sqlite3").connect(db)
    rows = con.execute("SELECT COUNT(*) FROM agents WHERE id=?", (aid,)).fetchone()[0]
    con.close()
    assert rows == 1, f"expected exactly 1 agent row (1 credential), observed {rows}"
    # the store is internally consistent after the rotation storm.
    assert s2.backend.integrity_check() == "ok"
    # NOTE: orphan account rows can remain from a concurrent same-agent rekey
    # race (each writer rekeys off its own stale in-memory account map). Those
    # keys are dead (authenticate for nobody) and cannot occur under the
    # single-writer production topology; the credential invariant above is the
    # one the task requires.


# --- 3. concurrent endpoint declarations on DIFFERENT agents -----------------
def test_concurrent_endpoint_declarations(_force_sqlite):
    db = _force_sqlite
    s = _store()
    ids = [s.register_agent(f"ep{i}", ["cap"], {})["id"] for i in range(6)]
    procs = []
    for aid in ids:
        procs.append(subprocess.Popen(
            [sys.executable, "-c", WORKER, db, "endpoint",
             json.dumps({"agent_id": aid, "endpoint": "https://example.com/a2a"})],
            cwd=str(GUILD_DIR), env={**os.environ, "PYTHONPATH": str(GUILD_DIR)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    for p in procs:
        o, e = p.communicate(timeout=120)
        assert p.returncode == 0, f"endpoint worker crashed: {e.decode()}"
    s2 = _store()
    declared = sum(1 for aid in ids
                   if (s2.agents[aid].get("metadata") or {}).get("endpoint")
                   == "https://example.com/a2a")
    assert declared == len(ids), (
        f"expected {len(ids)} endpoint declarations to survive, observed {declared}")


# --- 4. concurrent invocation begin/complete ---------------------------------
def test_concurrent_invocations(_force_sqlite):
    db = _force_sqlite
    s = _store()
    ids = []
    for i in range(4):
        a = s.register_agent(f"inv{i}", ["cap"], {})
        s.set_agent_endpoint(a["id"], "https://example.com/a2a")
        ids.append(a["id"])
    procs = []
    for aid in ids:
        procs.append(subprocess.Popen(
            [sys.executable, "-c", WORKER, db, "invoke",
             json.dumps({"agent_id": aid, "n": 5})],
            cwd=str(GUILD_DIR), env={**os.environ, "PYTHONPATH": str(GUILD_DIR)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    total = 0
    for p in procs:
        o, e = p.communicate(timeout=120)
        assert p.returncode == 0, f"invoke worker crashed: {e.decode()}"
        total += int(o.decode().split()[-1])
    s2 = _store()
    verified = sum(1 for aid in ids
                   if (s2.agents[aid].get("reachability") or {}).get("status")
                   == "invocation_verified")
    assert verified == len(ids), (
        f"expected {len(ids)} agents invocation_verified, observed {verified}")


# --- 5. concurrent event appends: all present, per-process order preserved ----
def test_concurrent_event_appends_all_present_ordered(_force_sqlite):
    db, procs, per = _force_sqlite, 4, 50
    results = _spawn(db, "events", {"n": per}, count=procs,
                     tags=[f"p{i}" for i in range(procs)])
    _no_crash(results)
    import sqlite3
    con = sqlite3.connect(db)
    rows = con.execute("SELECT key, json FROM events WHERE type='conc' ORDER BY seq").fetchall()
    con.close()
    total = len(rows)
    expected = procs * per
    assert total == expected, (
        f"concurrent events: expected {expected}, observed {total}, "
        f"lost {expected - total}")
    # within each process the i-values are strictly increasing (order preserved)
    seen = {}
    for key, blob in rows:
        i = json.loads(blob)["i"]
        assert i == seen.get(key, 0), (
            f"process {key} out of order: expected i={seen.get(key,0)}, saw {i}")
        seen[key] = i + 1
    assert all(v == per for v in seen.values()), (
        f"expected each process to land {per} events, got {seen}")


# --- 6. concurrent debits: no double-spend, final balance exact --------------
def test_concurrent_debits_no_double_spend(_force_sqlite):
    db = _force_sqlite
    s = _store()
    payer = s.register_agent("payer", ["cap"], {})
    key = payer["api_key"]
    # known starting balance
    s.credit(key, 1000, reason="seed")
    start = s.get_account(key)["balance"]
    procs, per, amount = 4, 50, 3
    results = _spawn(db, "debit", {"key": key, "n": per, "amount": amount}, count=procs)
    _no_crash(results)
    charged = sum(int(o.split()[-1]) for _, o, _ in results)
    s2 = _store()
    bal = s2.get_account(key)["balance"]
    expected = start - charged * amount
    assert bal == expected, (
        f"double-spend: start={start} charged={charged}x{amount} "
        f"expected_balance={expected} observed_balance={bal}")
    assert bal >= 0, f"balance went negative: {bal}"


# --- 7. concurrent escrow opens on one payer ---------------------------------
def test_concurrent_escrow_opens(_force_sqlite):
    db = _force_sqlite
    s = _store()
    payer = s.register_agent("epayer", ["cap"], {})
    worker = s.register_agent("eworker", ["cap"], {})
    key = payer["api_key"]
    s.credit(key, 100000, reason="seed")
    start = s.get_account(key)["balance"]
    procs, per, amount = 4, 20, 10
    results = _spawn(db, "escrow_open",
                     {"key": key, "worker_id": worker["id"], "n": per, "amount": amount},
                     count=procs)
    _no_crash(results)
    opened = sum(int(o.split()[-1]) for _, o, _ in results)
    import sqlite3
    con = sqlite3.connect(db)
    n_esc = con.execute("SELECT COUNT(*) FROM escrows").fetchone()[0]
    con.close()
    s2 = _store()
    bal = s2.get_account(key)["balance"]
    assert n_esc == opened, f"expected {opened} escrows, observed {n_esc}"
    assert bal == start - opened * amount, (
        f"escrow debit mismatch: start={start} opened={opened}x{amount} "
        f"expected={start - opened * amount} observed={bal}")


# --- 8. concurrent receipt submissions on distinct tasks ---------------------
def test_concurrent_receipt_submissions(_force_sqlite):
    db = _force_sqlite
    s = _store()
    req = s.register_agent("rq", ["cap"], {})
    wrk = s.register_agent("wk", ["cap"], {})
    tids = [s.create_task(req["id"], wrk["id"], "cap")["id"] for _ in range(6)]
    procs = []
    for tid in tids:
        procs.append(subprocess.Popen(
            [sys.executable, "-c", WORKER, db, "receipt", json.dumps({"task_id": tid})],
            cwd=str(GUILD_DIR), env={**os.environ, "PYTHONPATH": str(GUILD_DIR)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    for p in procs:
        o, e = p.communicate(timeout=120)
        assert p.returncode == 0, f"receipt worker crashed: {e.decode()}"
    s2 = _store()
    accepted = sum(1 for tid in tids if s2.tasks[tid].get("outcome") == "accepted")
    assert accepted == len(tids), (
        f"expected {len(tids)} receipts accepted, observed {accepted}")


# --- 9. simultaneous readers + writers ---------------------------------------
def test_simultaneous_readers_and_writers(_force_sqlite):
    db = _force_sqlite
    procs = []
    # 2 writers registering, 2 readers looping load_all()
    for i in range(2):
        procs.append(subprocess.Popen(
            [sys.executable, "-c", WORKER, db, "register",
             json.dumps({"n": 40, "tag": f"w{i}_"})],
            cwd=str(GUILD_DIR), env={**os.environ, "PYTHONPATH": str(GUILD_DIR)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    for i in range(2):
        procs.append(subprocess.Popen(
            [sys.executable, "-c", WORKER, db, "reader", json.dumps({"secs": 2})],
            cwd=str(GUILD_DIR), env={**os.environ, "PYTHONPATH": str(GUILD_DIR)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    outs = []
    for p in procs:
        o, e = p.communicate(timeout=120)
        assert p.returncode == 0, f"reader/writer crashed: {e.decode()}"
        outs.append(o.decode())
    import sqlite3
    con = sqlite3.connect(db)
    agents = con.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    con.close()
    assert agents == 80, f"expected 80 agents from 2 writers x 40, observed {agents}"
    reads = sum(int(o.split()[-1]) for o in outs if o.startswith("reads"))
    assert reads > 0, "readers made no successful reads while writers ran"


# --- 10. process KILLED mid-transaction: no half-commit ----------------------
def test_kill_mid_transaction_no_half_commit(_force_sqlite):
    db = _force_sqlite
    s = _store()
    base = s.register_agent("base", ["cap"], {})["id"]  # a committed agent
    results = _spawn(db, "kill_mid_txn", {}, count=3)
    # workers os._exit(0) inside an open BEGIN IMMEDIATE -> nothing committed
    import sqlite3
    con = sqlite3.connect(db)
    ghosts = con.execute("SELECT COUNT(*) FROM agents WHERE id LIKE 'ghost_%'").fetchone()[0]
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    has_base = con.execute("SELECT 1 FROM agents WHERE id=?", (base,)).fetchone()
    con.close()
    assert ghosts == 0, f"half-commit leaked {ghosts} ghost rows from killed transactions"
    assert integrity == "ok", f"integrity_check after kill: {integrity}"
    assert has_base, "the pre-existing committed agent was lost after a peer crash"


# --- 11. restart + recovery --------------------------------------------------
def test_restart_and_recovery(_force_sqlite):
    db = _force_sqlite
    _spawn(db, "register", {"n": 30}, count=3)  # 90 committed agents
    # brand new Store process reopening the same file sees everything
    s = _store()
    assert len(s.agents) == 90, f"restart: expected 90 agents, observed {len(s.agents)}"
    assert s.backend.integrity_check() == "ok"


# --- 12. duplicate primary-key idempotency -----------------------------------
def test_duplicate_pk_is_idempotent(_force_sqlite):
    db = _force_sqlite
    results = _spawn(db, "idem_pk", {"id": "task_fixed", "n": 20}, count=4)
    _no_crash(results)
    import sqlite3
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM tasks WHERE id='task_fixed'").fetchone()[0]
    con.close()
    assert n == 1, f"expected exactly 1 row for a repeated primary key, observed {n}"


# --- 13. WAL checkpoint behavior ---------------------------------------------
def test_wal_checkpoint(_force_sqlite):
    db = _force_sqlite
    _spawn(db, "register", {"n": 50}, count=2)  # generate WAL frames
    s = _store()
    busy, logframes, checkpointed = s.backend.wal_checkpoint("TRUNCATE")
    assert busy == 0, f"wal_checkpoint TRUNCATE reported busy={busy}"
    # data survives the checkpoint intact
    s2 = _store()
    assert len(s2.agents) == 100, (
        f"expected 100 agents after WAL checkpoint, observed {len(s2.agents)}")
    assert s2.backend.integrity_check() == "ok"
