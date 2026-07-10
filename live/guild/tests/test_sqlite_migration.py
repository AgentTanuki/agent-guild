"""Migration-readiness tests for live/scripts/migrate_json_to_sqlite.py.

Builds a small but representative store THROUGH the existing Store API (agents,
accounts/credits, a task + receipt, a matched attestation pair, a funded +
released escrow, instrumentation events including a journal-only one), migrates
it to SQLite, and asserts:

  * per-table row counts match the JSON source (journal merged, app dedup rule)
  * the hash-chained ledger re-verifies from the SQLite rows, and tampering
    with any migrated ledger row is detected
  * the migration never modifies the source JSON or the .events.jsonl sidecar
  * re-running the migration is idempotent (same counts, still verified)
  * --verify-only verifies without writing
  * two OS processes writing concurrently to the migrated SQLite file lose
    ZERO writes — the exact pattern that loses 50% of writes and crashes under
    the JSON store (docs/discovery-swarm/harness/results/concurrency_results.txt)
"""
import hashlib
import importlib.util
import json
import pathlib
import sqlite3
import subprocess
import sys

import pytest

from app.store import Store

SCRIPT_PATH = (pathlib.Path(__file__).resolve().parents[2]
               / "scripts" / "migrate_json_to_sqlite.py")


def _load_script():
    spec = importlib.util.spec_from_file_location("migrate_json_to_sqlite", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load_script()


def _sha(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


@pytest.fixture()
def populated(tmp_path):
    """A store built purely via the public Store API, persisted to JSON, with
    at least one event that lives ONLY in the .events.jsonl sidecar."""
    data = str(tmp_path / "guild.json")
    store = Store(path=data)
    req = store.register_agent("requester", ["orchestrate"], {"note": "test"})
    wrk = store.register_agent("worker", ["fact.check"], {})
    ref = store.register_agent("referred", ["summarize"], {},
                               referred_by=wrk["id"])
    # credits + billing
    store.credit(req["api_key"], 50, reason="topup")
    # task lifecycle -> receipt (dual-writes a ledger `receipt` entry)
    task = store.create_task(req["id"], wrk["id"], "fact.check", payment=5.0)
    store.submit_receipt(task["id"], "0x" + "ab" * 32)
    # matched attestation pair (dual-writes ledger `attestation` entries)
    store.add_custodial_attestation(req, wrk, "fact.check", 0.9, task["id"], "solid")
    store.add_custodial_attestation(wrk, req, "fact.check", 0.8, task["id"], "paid fast")
    # escrow: fund + release (settlement fee -> guild_revenue; more chain entries)
    esc = store.open_escrow(req["api_key"], wrk["id"], 40, capability="fact.check")
    store.release_escrow(esc["id"], req["api_key"])
    # a published checkpoint (lands in kv). NOTE: this (and durable_ledger)
    # backfills + _save()s, which compacts the events journal — so the
    # journal-only probe below must come after BOTH.
    store.publish_checkpoint()
    assert store.durable_ledger().verify_chain()
    # instrumentation event recorded AFTER the last _save: exists only in the
    # sidecar journal, never in guild.json — the migration must pick it up.
    store.record_event(req["api_key"], "journal_only_probe", probe=True)
    assert not (pathlib.Path(data + ".events.jsonl").read_text().strip() == "")
    return {"data": data, "store": store, "req": req, "wrk": wrk, "ref": ref,
            "db": str(tmp_path / "guild.sqlite3")}


def test_migration_counts_and_ledger_integrity(populated):
    data, db, store = populated["data"], populated["db"], populated["store"]
    src_sha = _sha(data)
    journal = data + ".events.jsonl"
    journal_sha = _sha(journal)

    counts = mig.migrate(data, db)

    # sources untouched, byte for byte
    assert _sha(data) == src_sha
    assert _sha(journal) == journal_sha

    # counts match the live Store's in-memory truth
    assert counts["agents"] == len(store.agents) == 3
    assert counts["accounts"] == len(store.accounts)
    assert counts["tasks"] == len(store.tasks)          # incl. escrow settlement task
    assert counts["attestations"] == len(store.attestations)
    assert counts["escrows"] == len(store.escrows) == 1
    assert counts["ledger"] == len(store.ledger_records) > 0
    # journal-only event must be present in SQLite
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM events WHERE type='journal_only_probe'").fetchone()[0]
    assert n == 1
    # ledger chain re-verifies FROM the SQLite rows
    rows = [json.loads(r[0]) for r in
            con.execute("SELECT data FROM ledger ORDER BY seq").fetchall()]
    ok, msg = mig.verify_ledger_chain(rows)
    assert ok, msg
    # heads agree with the source chain
    assert rows[-1]["hash"] == store.ledger_records[-1]["hash"]
    # escrow economics survived: released escrow + settled amounts
    status, amount, fee = con.execute(
        "SELECT status, amount, fee FROM escrows").fetchone()
    assert status == "released" and amount == 40 and fee >= 1
    # WAL is actually enabled on the produced file
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    con.close()
    # full independent verification passes
    assert mig.verify(data, db) is True


def test_migration_is_idempotent_and_verify_only_works(populated):
    data, db = populated["data"], populated["db"]
    first = mig.migrate(data, db)
    second = mig.migrate(data, db)
    assert first == second
    assert mig.verify(data, db) is True
    # CLI: --verify-only exits 0 and writes nothing to the db
    db_sha = _sha(db)
    assert mig.main(["--data", data, "--out", db, "--verify-only"]) == 0
    assert _sha(db) == db_sha


def test_tampered_ledger_row_fails_verification(populated):
    data, db = populated["data"], populated["db"]
    mig.migrate(data, db)
    con = sqlite3.connect(db)
    raw, seq = con.execute(
        "SELECT data, seq FROM ledger ORDER BY seq LIMIT 1").fetchone()
    rec = json.loads(raw)
    rec["created_at"] = "1999-01-01T00:00:00+00:00"   # rewrite history
    con.execute("UPDATE ledger SET data=? WHERE seq=?",
                (json.dumps(rec, separators=(",", ":")), seq))
    con.commit()
    con.close()
    assert mig.verify(data, db) is False
    assert mig.main(["--data", data, "--out", db, "--verify-only"]) == 1


def test_refuses_to_write_over_source_files(populated):
    data = populated["data"]
    with pytest.raises(ValueError):
        mig.migrate(data, data)
    with pytest.raises(ValueError):
        mig.migrate(data, data + ".events.jsonl")


# One writer process: N committed inserts into the shared events table.
_WRITER = """
import json, sqlite3, sys
db, tag, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
con = sqlite3.connect(db, timeout=30)
con.execute("PRAGMA busy_timeout=30000")
for i in range(n):
    at = "2026-07-10T00:00:00." + tag + ("%04d" % i)
    ev = {"at": at, "type": "conc_test", "key": tag, "fp": True, "i": i}
    with con:
        con.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                    (at, "conc_test", tag, 1, "", json.dumps(ev)))
con.close()
print("wrote", n)
"""


def test_two_process_concurrent_sqlite_writers_lose_zero_writes(populated):
    """Mirror the measured JSON failure (2 procs, 30 writes each -> crash and
    30 of 60 silently lost). Against migrated SQLite in WAL mode the same
    pattern must lose ZERO writes and neither process may error."""
    data, db = populated["data"], populated["db"]
    mig.migrate(data, db)
    n = 30
    procs = [subprocess.Popen([sys.executable, "-c", _WRITER, db, tag, str(n)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for tag in ("procA", "procB")]
    results = [p.communicate(timeout=120) for p in procs]
    for p, (out, err) in zip(procs, results):
        assert p.returncode == 0, f"writer crashed: {err.decode()}"
        assert b"wrote 30" in out
    con = sqlite3.connect(db)
    total = con.execute(
        "SELECT COUNT(*) FROM events WHERE type='conc_test'").fetchone()[0]
    per_tag = dict(con.execute(
        "SELECT key, COUNT(*) FROM events WHERE type='conc_test' GROUP BY key"))
    con.close()
    assert total == 2 * n, f"lost {2 * n - total} of {2 * n} writes"
    assert per_tag == {"procA": n, "procB": n}
    # and the migrated ledger around it is still chain-intact
    con = sqlite3.connect(db)
    rows = [json.loads(r[0]) for r in
            con.execute("SELECT data FROM ledger ORDER BY seq").fetchall()]
    con.close()
    ok, msg = mig.verify_ledger_chain(rows)
    assert ok, msg
