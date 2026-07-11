#!/usr/bin/env python3
"""Migrate the Guild's JSON-file store to SQLite (WAL) — offline, read-only on source.

Reads the GUILD_DATA JSON snapshot plus its append-only ``.events.jsonl``
sidecar journal (merged with the same ``(at, type, key)`` dedup rule the app's
``Store._replay_event_journal`` uses) and writes a SQLite database with one
table per hot collection plus a ``kv`` table for everything else. The source
files are opened read-only and are NEVER written, truncated, renamed or locked.

Tables: agents, accounts, tasks, attestations, escrows, ledger, events, kv.
Every row keeps the full original record in a ``data`` JSON column (lossless),
with a few indexed columns lifted out for queries.

After migrating, the script independently re-verifies:
  * row counts in SQLite == record counts in the JSON source (per table)
  * the hash-chained ledger is intact when re-read FROM SQLITE — every entry's
    sha256 over its canonical body matches its stored ``hash`` and links to the
    previous entry's hash (genesis = 64 zeros), byte-identical to
    ``app/ledger.py::Ledger.verify_chain`` semantics.

Idempotent: re-running against the same source upserts by primary key and
converges to the same row counts. Exit codes: 0 = migrated+verified (or
verified with --verify-only), 1 = verification failure, 2 = usage error.

Usage:
    python3 migrate_json_to_sqlite.py [--data /data/guild.json] [--out /data/guild.sqlite3]
    python3 migrate_json_to_sqlite.py --verify-only [--data ...] [--out ...]

--data defaults to $GUILD_DATA; --out defaults to <data-stem>.sqlite3.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from typing import Any, Optional

GENESIS = "0" * 64

# Collections that get their own table (everything else lands in kv).
KV_KEYS = ("billing_log", "referrals", "health_log", "identity", "checkpoints",
           "guild_revenue", "demand_watches", "swarm_state")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    did         TEXT,
    name        TEXT,
    first_party INTEGER,
    seed        INTEGER,
    custodial   INTEGER,
    created_at  TEXT,
    data        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    key            TEXT PRIMARY KEY,
    owner_agent_id TEXT,
    balance        INTEGER,
    spent          INTEGER,
    topped_up      INTEGER,
    first_party    INTEGER,
    created_at     TEXT,
    data           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id                 TEXT PRIMARY KEY,
    requester_agent_id TEXT,
    worker_agent_id    TEXT,
    task_type          TEXT,
    outcome            TEXT,
    payment            REAL,
    deliverable_hash   TEXT,
    created_at         TEXT,
    data               TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attestations (
    id         TEXT PRIMARY KEY,
    issuer_id  TEXT,
    subject_id TEXT,
    capability TEXT,
    rating     REAL,
    task_id    TEXT,
    verified   INTEGER,
    created_at TEXT,
    data       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS escrows (
    id            TEXT PRIMARY KEY,
    requester_key TEXT,
    requester_id  TEXT,
    worker_id     TEXT,
    capability    TEXT,
    amount        INTEGER,
    fee           INTEGER,
    status        TEXT,
    task_id       TEXT,
    created_at    TEXT,
    data          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    seq        INTEGER PRIMARY KEY,
    id         TEXT,
    type       TEXT,            -- NULL for legacy collaboration records
    task_id    TEXT,
    hash       TEXT UNIQUE,
    prev_hash  TEXT,
    created_at TEXT,
    data       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    at   TEXT,
    type TEXT,
    key  TEXT,
    fp   INTEGER,
    ua   TEXT,
    data TEXT NOT NULL
);
-- Same dedup identity the app uses in Store._replay_event_journal.
CREATE UNIQUE INDEX IF NOT EXISTS events_identity ON events (at, type, key);
CREATE INDEX IF NOT EXISTS events_type_at ON events (type, at);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_worker ON tasks (worker_agent_id);
CREATE INDEX IF NOT EXISTS attestations_subject ON attestations (subject_id);
CREATE INDEX IF NOT EXISTS ledger_task ON ledger (task_id);
"""


def canonicalize(value: Any) -> str:
    """Byte-identical to app/crypto.py::canonicalize (sorted keys, no whitespace)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _j(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


# --- source loading (READ-ONLY: files are opened with mode "r" only) ---------

def load_source(data_path: str) -> dict[str, Any]:
    """Load the JSON snapshot + merge the events journal, mirroring
    Store._load / Store._replay_event_journal exactly."""
    if not os.path.exists(data_path):
        raise FileNotFoundError(f"GUILD_DATA file not found: {data_path}")
    with open(data_path, "r") as f:
        try:
            data = json.load(f)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"malformed JSON source ({data_path}): {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"incomplete JSON source: top level is {type(data).__name__}, "
                         "expected an object with the store collections")
    state = {
        "agents": data.get("agents", {}),
        "tasks": data.get("tasks", {}),
        "attestations": data.get("attestations", []),
        "accounts": data.get("accounts", {}),
        "billing_log": data.get("billing_log", []),
        "events": list(data.get("events", [])),
        "referrals": data.get("referrals", []),
        "health_log": data.get("health_log", []),
        "identity": data.get("identity", {}),
        "ledger_records": data.get("ledger_records", []),
        "checkpoints": data.get("checkpoints", []),
        "escrows": data.get("escrows", {}),
        "guild_revenue": data.get("guild_revenue", 0),
        "demand_watches": data.get("demand_watches", []),
        "swarm_state": data.get("swarm_state", {}),
    }
    journal = data_path + ".events.jsonl"
    journal_appended = 0
    if os.path.exists(journal):
        seen = {(e.get("at"), e.get("type"), e.get("key")) for e in state["events"]}
        with open(journal, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue  # torn write at crash — same skip as the app
                ident = (e.get("at"), e.get("type"), e.get("key"))
                if ident not in seen:
                    seen.add(ident)
                    state["events"].append(e)
                    journal_appended += 1
    state["events"].sort(key=lambda e: e.get("at") or "")
    # a second dedup pass so SQLite's UNIQUE(at,type,key) can be compared to an
    # exact expected count even if the main file itself carries duplicates
    dedup, seen2 = [], set()
    for e in state["events"]:
        ident = (e.get("at"), e.get("type"), e.get("key"))
        if ident in seen2:
            continue
        seen2.add(ident)
        dedup.append(e)
    state["events_deduped"] = dedup
    state["_journal_appended"] = journal_appended
    return state


# --- ledger chain verification (standalone re-implementation) ----------------

def verify_ledger_chain(records: list[dict[str, Any]]) -> tuple[bool, str]:
    """Recompute every entry hash and prev-link, exactly like
    app/ledger.py::Ledger.verify_chain: hash = sha256(canonical(body)) where
    body is the record minus its `hash` and `id` fields; each entry's prev_hash
    must equal the previous entry's hash (genesis = 64 zeros)."""
    prev = GENESIS
    for i, d in enumerate(sorted(records, key=lambda r: r.get("seq", 0))):
        if d.get("seq") != i:
            return False, f"seq gap at position {i} (found seq={d.get('seq')})"
        if d.get("prev_hash") != prev:
            return False, f"broken link at seq {i}: prev_hash != prior hash"
        body = {k: v for k, v in d.items() if k not in ("hash", "id")}
        if _sha(canonicalize(body)) != d.get("hash"):
            return False, f"hash mismatch at seq {i} (id={d.get('id')})"
        prev = d["hash"]
    return True, f"chain intact: {len(records)} entries, head={prev[:12]}"


# --- migration ----------------------------------------------------------------

def _connect(db_path: str, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    else:
        con = sqlite3.connect(db_path, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")       # persistent: readers never block the writer
        con.execute("PRAGMA synchronous=FULL")       # durable through power loss
        con.execute("PRAGMA busy_timeout=30000")
    return con


def migrate(data_path: str, db_path: str) -> dict[str, int]:
    """Copy the JSON store into SQLite. Upserts by primary key (idempotent).
    Returns per-table row counts written this run's end state."""
    src_abs = os.path.abspath(data_path)
    for banned in (src_abs, src_abs + ".events.jsonl", src_abs + ".tmp"):
        if os.path.abspath(db_path) == banned:
            raise ValueError("refusing to write SQLite over a source file: " + banned)
    state = load_source(data_path)
    con = _connect(db_path)
    try:
        con.executescript(SCHEMA)
        with con:  # one transaction — all-or-nothing
            for a in state["agents"].values():
                con.execute(
                    "INSERT OR REPLACE INTO agents VALUES (?,?,?,?,?,?,?,?)",
                    (a.get("id"), a.get("did"), a.get("name"),
                     int(bool(a.get("first_party"))), int(bool(a.get("seed"))),
                     int(bool(a.get("custodial"))), a.get("created_at"), _j(a)))
            for k, acct in state["accounts"].items():
                con.execute(
                    "INSERT OR REPLACE INTO accounts VALUES (?,?,?,?,?,?,?,?)",
                    (k, acct.get("owner_agent_id"), acct.get("balance"),
                     acct.get("spent"), acct.get("topped_up"),
                     int(bool(acct.get("first_party"))), acct.get("created_at"), _j(acct)))
            for t in state["tasks"].values():
                con.execute(
                    "INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
                    (t.get("id"), t.get("requester_agent_id"), t.get("worker_agent_id"),
                     t.get("task_type"), t.get("outcome"), t.get("payment"),
                     t.get("deliverable_hash"), t.get("created_at"), _j(t)))
            for att in state["attestations"]:
                con.execute(
                    "INSERT OR REPLACE INTO attestations VALUES (?,?,?,?,?,?,?,?,?)",
                    (att.get("id"), att.get("issuer_id"), att.get("subject_id"),
                     att.get("capability"), att.get("rating"), att.get("task_id"),
                     int(bool(att.get("verified"))), att.get("created_at"), _j(att)))
            for e in state["escrows"].values():
                con.execute(
                    "INSERT OR REPLACE INTO escrows VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (e.get("id"), e.get("requester_key"), e.get("requester_id"),
                     e.get("worker_id"), e.get("capability"), e.get("amount"),
                     e.get("fee"), e.get("status"), e.get("task_id"),
                     e.get("created_at"), _j(e)))
            for d in state["ledger_records"]:
                con.execute(
                    "INSERT OR REPLACE INTO ledger VALUES (?,?,?,?,?,?,?,?)",
                    (d.get("seq"), d.get("id"), d.get("type"), d.get("task_id"),
                     d.get("hash"), d.get("prev_hash"), d.get("created_at"), _j(d)))
            for ev in state["events_deduped"]:
                con.execute(
                    "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)",
                    (ev.get("at"), ev.get("type"), ev.get("key"),
                     int(bool(ev.get("fp"))), ev.get("ua", ""), _j(ev)))
            for k in KV_KEYS:
                con.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, _j(state[k])))
            with open(data_path, "rb") as f:
                src_sha = hashlib.sha256(f.read()).hexdigest()
            con.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", ("_migration_meta", _j({
                "source_path": src_abs,
                "source_sha256": src_sha,
                "journal_events_appended": state["_journal_appended"],
                "schema_version": 1,
            })))
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("agents", "accounts", "tasks", "attestations",
                          "escrows", "ledger", "events", "kv")}
    finally:
        con.close()


# --- verification ---------------------------------------------------------------

# --- extended integrity checks (orphans, dups, hashes, auth, reachability) ----

def _record_hashes(records, key):
    """sha256(canonical(record)) keyed by `key` (a field name)."""
    return {r.get(key): _sha(canonicalize(r)) for r in records}


def check_record_hashes(state, con) -> tuple[bool, list[str]]:
    """Canonical-hash comparison for the critical entities (agents, ledger):
    every source record must hash byte-identically to its migrated json."""
    msgs, ok = [], True
    # agents (keyed by id)
    src = _record_hashes(list(state["agents"].values()), "id")
    db = {json.loads(r[1]).get("id"): _sha(canonicalize(json.loads(r[1])))
          for r in con.execute("SELECT id, data FROM agents").fetchall()}
    if src == db:
        msgs.append(f"[OK ] agent canonical hashes match ({len(src)})")
    else:
        ok = False
        msgs.append(f"[FAIL] agent canonical hash mismatch "
                    f"(source={len(src)} sqlite={len(db)})")
    # ledger (keyed by seq)
    ssrc = _record_hashes(state["ledger_records"], "seq")
    sdb = {json.loads(r[0]).get("seq"): _sha(canonicalize(json.loads(r[0])))
           for r in con.execute("SELECT data FROM ledger").fetchall()}
    if ssrc == sdb:
        msgs.append(f"[OK ] ledger canonical hashes match ({len(ssrc)})")
    else:
        ok = False
        msgs.append(f"[FAIL] ledger canonical hash mismatch")
    return ok, msgs


def check_orphans(state) -> tuple[bool, list[str]]:
    """Referential integrity: accounts->agents, escrows->agents."""
    agent_ids = set(state["agents"])
    orphan_acc = [k for k, a in state["accounts"].items()
                  if a.get("owner_agent_id") and a["owner_agent_id"] not in agent_ids]
    orphan_esc = [e.get("id") for e in state["escrows"].values()
                  if (e.get("requester_id") and e["requester_id"] not in agent_ids)
                  or (e.get("worker_id") and e["worker_id"] not in agent_ids)]
    ok = not orphan_acc and not orphan_esc
    msgs = [f"[{'OK ' if not orphan_acc else 'FAIL'}] account->agent orphans: {len(orphan_acc)}",
            f"[{'OK ' if not orphan_esc else 'FAIL'}] escrow->agent orphans: {len(orphan_esc)}"]
    return ok, msgs


def check_duplicates(state, con) -> tuple[bool, list[str]]:
    """No duplicate primary keys, in the source lists or the migrated tables."""
    msgs, ok = [], True
    seqs = [d.get("seq") for d in state["ledger_records"]]
    att_ids = [a.get("id") for a in state["attestations"]]
    for label, vals in (("ledger.seq", seqs), ("attestation.id", att_ids)):
        dups = len(vals) - len(set(vals))
        if dups:
            ok = False
        msgs.append(f"[{'OK ' if not dups else 'FAIL'}] duplicate {label}: {dups}")
    # every migrated PK table has exactly COUNT(DISTINCT pk) == COUNT(*)
    for table, pk in (("agents", "id"), ("accounts", "key"), ("tasks", "id"),
                      ("escrows", "id"), ("ledger", "seq")):
        tot = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        dis = con.execute(f"SELECT COUNT(DISTINCT {pk}) FROM {table}").fetchone()[0]
        if tot != dis:
            ok = False
            msgs.append(f"[FAIL] {table}.{pk} has duplicates ({tot} rows, {dis} distinct)")
    return ok, msgs


def _load_creds():
    """Best-effort import of app.credentials so raw-key auth can be tested
    end-to-end where a raw key is available. Returns None if unavailable (in
    which case raw-key auth is reported as NOT performed, never as passed)."""
    try:
        import app.credentials as c  # already on the path?
        return c
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    guild = os.path.join(os.path.dirname(here), "guild")
    if guild not in sys.path:
        sys.path.insert(0, guild)
    try:
        import app.credentials as c
        return c
    except Exception:
        return None


def check_credential_auth(state, con, test_keys=None) -> tuple[bool, list[str]]:
    """Credential verification, reported HONESTLY and SEPARATELY.

    Migration preserves a credential byte-identically; it does NOT (and cannot)
    re-derive a *salted* PBKDF2 verifier from a hash. So this reports four
    distinct things, and NEVER claims credential-auth verification where no raw
    key exists:

      (a) verifier-format preservation — the ``api_key_hash`` (``pbkdf2_sha256$..``)
          / plaintext ``api_key`` / ``scopes`` / revoked / expiry fields survive
          byte-identically into sqlite;
      (b) ``key_id`` preservation;
      (c) raw-key END-TO-END authentication against the MIGRATED record —
          performed ONLY where a raw key is available (a plaintext key still in
          the source, or a ``--test-key`` supplied on the CLI);
      (d) the COUNT of records that CANNOT be end-to-end authenticated at
          migration time (hashed-at-rest, no raw key)."""
    creds = _load_creds()
    fields_fmt = ("api_key", "api_key_hash", "key_id", "scopes",
                  "api_key_revoked_at", "api_key_expires_at")
    agents = list(state["agents"].values())
    msgs, ok = [], True

    # (a) + (b): format + key_id survive byte-identically into sqlite.
    fmt_ok = kid_ok = True
    have_cred = 0
    for a in agents:
        if not any(a.get(f) for f in ("api_key", "api_key_hash", "key_id")):
            continue
        have_cred += 1
        row = con.execute("SELECT data FROM agents WHERE id=?", (a["id"],)).fetchone()
        mig = json.loads(row[0]) if row else {}
        if any(mig.get(f) != a.get(f) for f in fields_fmt):
            fmt_ok = False
        if mig.get("key_id") != a.get("key_id"):
            kid_ok = False
    ok = ok and fmt_ok and kid_ok
    msgs.append(f"[{'OK ' if fmt_ok else 'FAIL'}] verifier-format preserved byte-identically "
                f"(api_key/api_key_hash/scopes/revoked/expiry) for {have_cred} credential(s)")
    msgs.append(f"[{'OK ' if kid_ok else 'FAIL'}] key_id preserved for {have_cred} credential(s)")

    # (c): assemble the set of agents for which a RAW key is actually available.
    raw_by_agent = {}
    for a in agents:
        raw = a.get("api_key")
        if isinstance(raw, str) and raw.startswith("sk_"):
            raw_by_agent[a["id"]] = raw     # plaintext-at-rest source key
    if creds is not None:
        for raw in (test_keys or []):
            try:
                kid = creds.key_id_of(raw)
            except Exception:
                continue
            for a in agents:
                if a.get("key_id") == kid or a.get("api_key") == raw:
                    raw_by_agent[a["id"]] = raw   # operator-supplied known key

    tested = passed = 0
    if raw_by_agent and creds is None:
        msgs.append("[WARN] app.credentials not importable — raw-key auth NOT performed")
    elif creds is not None:
        for aid, raw in raw_by_agent.items():
            row = con.execute("SELECT data FROM agents WHERE id=?", (aid,)).fetchone()
            mig = json.loads(row[0]) if row else {}
            tested += 1
            stored = mig.get("api_key_hash")
            if stored:
                authed = creds.verify_key_hash(raw, stored)
            else:
                authed = bool(mig.get("api_key")) and mig.get("api_key") == raw
            kid_match = (not mig.get("key_id")) or creds.key_id_of(raw) == mig.get("key_id")
            if authed and kid_match:
                passed += 1
            else:
                ok = False
    if tested:
        msgs.append(f"[{'OK ' if passed == tested else 'FAIL'}] raw-key END-TO-END auth "
                    f"against the MIGRATED record: {passed}/{tested} raw key(s) authenticate")
    else:
        msgs.append("[INFO] raw-key END-TO-END auth: 0 performed (no plaintext key in "
                    "source and no --test-key supplied) — NOT claiming auth verification")

    # (d): what remains unverifiable at migration time.
    hashed_no_raw = sum(1 for a in agents
                        if a.get("api_key_hash") and a["id"] not in raw_by_agent)
    msgs.append(f"[INFO] {hashed_no_raw} credential(s) hashed-at-rest with NO raw key "
                "available -> CANNOT be end-to-end authenticated during migration "
                "(verifier-format preservation is the only guarantee; pass --test-key "
                "to end-to-end verify a known key)")
    return ok, msgs


def check_reachability(state, con) -> tuple[bool, list[str]]:
    """Every agent's reachability sub-record survives migration byte-identically."""
    have = [a for a in state["agents"].values() if a.get("reachability")]
    ok = True
    for a in have:
        row = con.execute("SELECT data FROM agents WHERE id=?", (a["id"],)).fetchone()
        mig = json.loads(row[0]) if row else {}
        if mig.get("reachability") != a.get("reachability"):
            ok = False
    return ok, [f"[{'OK ' if ok else 'FAIL'}] reachability records preserved "
                f"({len(have)} agents carry a reachability record)"]



# --- complete collection matrix (all 16 store collections) -------------------

# Every current Store collection, and how the migration represents it. This is
# the migrate-script view; the RUNTIME backend (app/store_sqlite.py) is
# documented in docs/discovery-swarm/SQLITE_SCHEMA.md.
ALL_COLLECTIONS = (
    ("agents",               "table"),
    ("tasks",                "table"),
    ("attestations",         "table"),
    ("accounts",             "table"),
    ("escrows",              "table"),
    ("ledger_records",       "ledger-table"),
    ("events",               "events-table"),
    ("billing_log",          "kv-list"),
    ("referrals",            "kv-list"),
    ("health_log",           "kv-list"),
    ("checkpoints",          "kv-list"),
    ("demand_watches",       "kv-list"),
    ("identity",             "kv-value"),
    ("swarm_state",          "kv-value"),
    ("guild_revenue",        "kv-value"),
    ("outbound_invocations", "transient"),
)


def verify_all_collections(state, con) -> tuple[bool, list[str]]:
    """Compare EVERY one of the 16 store collections (not just agents+ledger)."""
    msgs, ok = [], []
    table_of = {"agents": "agents", "tasks": "tasks", "attestations": "attestations",
                "accounts": "accounts", "escrows": "escrows",
                "ledger_records": "ledger", "events": "events"}
    for name, kind in ALL_COLLECTIONS:
        if kind in ("table", "ledger-table", "events-table"):
            table = table_of[name]
            got = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            want = (len(state["events_deduped"]) if name == "events"
                    else len(state[name]))
            good = got == want
            msgs.append(f"[{'OK ' if good else 'FAIL'}] {name:<20} -> {table:<12} "
                        f"sqlite={got:<7} source={want}")
        elif kind == "kv-list":
            row = con.execute("SELECT v FROM kv WHERE k=?", (name,)).fetchone()
            got = len(json.loads(row[0])) if row else -1
            want = len(state[name])
            good = row is not None and json.loads(row[0]) == state[name]
            msgs.append(f"[{'OK ' if good else 'FAIL'}] {name:<20} -> kv          "
                        f"sqlite={got:<7} source={want} (full-equality)")
        elif kind == "kv-value":
            row = con.execute("SELECT v FROM kv WHERE k=?", (name,)).fetchone()
            good = row is not None and json.loads(row[0]) == state[name]
            msgs.append(f"[{'OK ' if good else 'FAIL'}] {name:<20} -> kv          "
                        f"scalar/dict equality")
        else:  # transient — the JSON store never persists outbound_invocations
            good = True
            msgs.append(f"[OK ] {name:<20} -> (transient) NOT persisted by the JSON "
                        f"store; 0 rows expected pre-cutover")
        ok.append(good)
    return all(ok), msgs


def verify(data_path: str, db_path: str, test_keys=None) -> bool:
    """Independently compare SQLite contents against the JSON source and
    re-verify the ledger hash chain from the SQLITE rows. Read-only on both."""
    state = load_source(data_path)
    con = _connect(db_path, readonly=True)
    ok = True
    try:
        expected = {
            "agents": len(state["agents"]),
            "accounts": len(state["accounts"]),
            "tasks": len(state["tasks"]),
            "attestations": len(state["attestations"]),
            "escrows": len(state["escrows"]),
            "ledger": len(state["ledger_records"]),
            "events": len(state["events_deduped"]),
        }
        for table, want in expected.items():
            got = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            status = "OK " if got == want else "FAIL"
            if got != want:
                ok = False
            print(f"  [{status}] {table:<13} sqlite={got:<8} source={want}")
        # complete collection matrix — EVERY one of the 16 store collections
        print("  -- all 16 collections --")
        coll_ok, coll_msgs = verify_all_collections(state, con)
        for m in coll_msgs:
            print("  " + m)
        ok = ok and coll_ok
        # ledger chain, re-read from SQLite (not from the source)
        rows = con.execute("SELECT data FROM ledger ORDER BY seq").fetchall()
        db_records = [json.loads(r[0]) for r in rows]
        chain_ok, msg = verify_ledger_chain(db_records)
        print(f"  [{'OK ' if chain_ok else 'FAIL'}] ledger chain (from sqlite): {msg}")
        ok = ok and chain_ok
        # and confirm the source chain agrees on the same head
        src_ok, src_msg = verify_ledger_chain(state["ledger_records"])
        print(f"  [{'OK ' if src_ok else 'FAIL'}] ledger chain (from source): {src_msg}")
        ok = ok and src_ok
        if db_records and state["ledger_records"]:
            same_head = db_records[-1].get("hash") == state["ledger_records"][-1].get("hash")
            print(f"  [{'OK ' if same_head else 'FAIL'}] chain heads match")
            ok = ok and same_head
        # extended integrity: canonical hashes, orphans, dups, auth, reachability
        for fn in (check_record_hashes, check_orphans, check_duplicates,
                   check_credential_auth, check_reachability):
            try:
                if fn is check_orphans:
                    sub_ok, msgs = fn(state)
                elif fn is check_credential_auth:
                    sub_ok, msgs = fn(state, con, test_keys)
                else:
                    sub_ok, msgs = fn(state, con)
            except Exception as exc:  # a check must never mask a real result
                sub_ok, msgs = False, [f"[FAIL] {fn.__name__}: {exc}"]
            for m in msgs:
                print("  " + m)
            ok = ok and sub_ok
        # every kv collection present
        for k in KV_KEYS:
            row = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
            if row is None or json.loads(row[0]) != state[k]:
                print(f"  [FAIL] kv[{k}] missing or differs from source")
                ok = False
        return ok
    finally:
        con.close()


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data", default=os.environ.get("GUILD_DATA", ""),
                   help="source JSON store (default: $GUILD_DATA)")
    p.add_argument("--out", default="",
                   help="destination SQLite file (default: <data-stem>.sqlite3)")
    p.add_argument("--verify-only", action="store_true",
                   help="verify an existing SQLite file against the source; write nothing")
    p.add_argument("--test-key", action="append", default=[], metavar="RAW_KEY",
                   help="a RAW sk_ credential to END-TO-END authenticate against its "
                        "migrated record (repeatable). The ONLY way to prove a "
                        "hashed-at-rest credential still authenticates post-migration.")
    args = p.parse_args(argv)
    if not args.data:
        print("error: no source given (--data or GUILD_DATA)", file=sys.stderr)
        return 2
    out = args.out or (os.path.splitext(args.data)[0] + ".sqlite3")
    try:
        if args.verify_only:
            if not os.path.exists(out):
                print(f"error: --verify-only but {out} does not exist", file=sys.stderr)
                return 2
            print(f"verify-only: {out} vs {args.data}")
            return 0 if verify(args.data, out, args.test_key) else 1
        print(f"migrating {args.data} -> {out}")
        pre = load_source(args.data)
        print("pre-migration source counts:")
        for t in ("agents", "accounts", "tasks", "attestations", "escrows",
                  "ledger_records", "events_deduped"):
            print(f"  {t:<16} {len(pre[t])}")
        counts = migrate(args.data, out)
        print("post-migration sqlite counts:")
        for t, n in counts.items():
            print(f"  wrote {t:<13} {n}")
        print("verifying...")
        good = verify(args.data, out, args.test_key)
        print("RESULT:", "verified OK" if good else "VERIFICATION FAILED")
        print("")
        print("ROLLBACK: this migration is non-destructive — the JSON source and "
              "its .events.jsonl journal are never modified. To roll back, simply "
              "keep running with the JSON store (unset GUILD_STORE, or "
              "GUILD_STORE=json) and delete the generated file:")
        print(f"    rm -f {out} {out}-wal {out}-shm")
        return 0 if good else 1
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
