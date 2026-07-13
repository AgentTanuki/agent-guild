"""The activation path, proven end-to-end on real files:

  JSON store → (deploy flips GUILD_STORE=sqlite) → automatic backup + migration
  → restart survival under sqlite → ROLLBACK (flip back to json) serves the
  pre-cutover state untouched.

This is the local twin of the production canary (live/scripts/deploy_canary.py)."""
import json
import os
import glob

import pytest
from app.store import Store


@pytest.fixture()
def data_path(tmp_path, monkeypatch):
    p = tmp_path / "guild.json"
    monkeypatch.delenv("GUILD_STORE_PATH", raising=False)
    return str(p)


def _fill(s: Store):
    a = s.register_agent("R", ["h"], metadata={})
    b = s.register_agent("W", ["x"], metadata={})
    t = s.create_task(a["id"], b["id"], "x")
    s.submit_receipt(t["id"], "0x" + "aa" * 32, outcome="accepted",
                     receipt_auth="worker_key")
    s.add_custodial_attestation(a, b, "x", 1.0, t["id"], "c")
    s.ensure_ledger_backfilled()
    s.append_task_to_ledger(t["id"])
    return a, b, t


def test_json_to_sqlite_migration_backup_restart_rollback(data_path, monkeypatch):
    # 1. life under JSON
    monkeypatch.setenv("GUILD_STORE", "json")
    s1 = Store(path=data_path)
    a, b, t = _fill(s1)
    json_agents = set(s1.agents)
    json_head = s1.ledger_records[-1]["hash"]

    # 2. deploy flips to sqlite: automatic backup + migration on first boot
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    s2 = Store(path=data_path)
    assert set(s2.agents) == json_agents
    assert s2.ledger_records[-1]["hash"] == json_head
    backups = glob.glob(data_path + ".pre-sqlite-*")
    assert backups, "pre-migration backup missing"
    assert json.loads(open(backups[0]).read())["agents"].keys() == json_agents

    # 3. new evidence lands under sqlite…
    c = s2.register_agent("NewUnderSqlite", ["y"], metadata={})
    from app.ledger import Ledger
    assert Ledger.from_records(s2.ledger_records).verify_chain()

    # 4. …and SURVIVES a process restart (fresh Store, same env)
    s3 = Store(path=data_path)
    assert c["id"] in s3.agents
    assert set(json_agents) <= set(s3.agents)
    assert Ledger.from_records(s3.ledger_records).verify_chain()

    # 5. ROLLBACK: flip back to json — pre-cutover state serves untouched
    monkeypatch.setenv("GUILD_STORE", "json")
    s4 = Store(path=data_path)
    assert set(s4.agents) == json_agents          # exactly the pre-cutover world
    assert s4.ledger_records[-1]["hash"] == json_head
    assert c["id"] not in s4.agents               # sqlite-era writes are in sqlite,
                                                  # not lost — flip forward again:
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    s5 = Store(path=data_path)
    assert c["id"] in s5.agents


def test_credential_hashing_migrates_in_place(data_path, monkeypatch):
    monkeypatch.setenv("GUILD_STORE", "json")
    monkeypatch.delenv("GUILD_HASH_KEYS", raising=False)
    s1 = Store(path=data_path)
    a = s1.register_agent("Legacy", ["x"], metadata={})
    raw = a["api_key"]
    assert raw.startswith("sk_")

    monkeypatch.setenv("GUILD_HASH_KEYS", "1")
    monkeypatch.setenv("GUILD_ALLOW_WEAK_KDF", "1")
    s2 = Store(path=data_path)
    rec = s2.agents[a["id"]]
    # plaintext gone, verifier present, raw key still authenticates
    assert not (rec.get("api_key") or "").startswith("sk_")
    from app import credentials as creds
    assert creds.verify_agent_key(rec, raw)
    assert not creds.verify_agent_key(rec, "sk_wrong")
