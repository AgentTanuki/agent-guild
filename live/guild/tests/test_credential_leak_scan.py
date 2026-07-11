"""Repository-wide credential-leak regression scan (credential-hardening).

Under GUILD_HASH_KEYS=1 a raw secret (sk_...) must never come to rest in:
JSON state, the events journal, event records, the billing log, application
logs, or exception strings. Also a static scan of the repo for stray raw-key
literals outside tests. And a sanity check that the production KDF cost is
bounded so auth latency stays reasonable.
"""
import json, logging, os, re, subprocess, tempfile, time

import pytest

os.environ.setdefault("GUILD_DATA", os.path.join(tempfile.mkdtemp(), "g.json"))

import app.credentials as creds
from app.store import Store

SK = re.compile(r"sk_[0-9a-f]{16,}")


@pytest.fixture
def hash_on(monkeypatch):
    monkeypatch.setenv("GUILD_HASH_KEYS", "1")
    monkeypatch.setenv("GUILD_KDF_ITERS", "1000")
    monkeypatch.setenv("GUILD_ALLOW_WEAK_KDF", "1")   # fast for tests
    yield


def _drive(store):
    """Exercise every surface that could capture a raw key, incl. failure paths."""
    a = store.register_agent("LeakProbe", ["x"], {})
    raw = a["api_key"]
    store.record_event(raw, "query", ua="x", endpoint="test")        # raw actor key
    store.record_event("sk_deadbeefdeadbeef00", "query", ua="probe") # unknown raw key
    try:
        store.open_escrow(raw, "agent_missing", 5, "cap", {})        # may raise
    except Exception:
        pass
    store.rotate_api_key(a["id"])
    store.revoke_api_key(a["id"])
    return raw


def test_no_raw_key_in_state_journal_or_events(hash_on):
    path = os.path.join(tempfile.mkdtemp(), "leak.json")
    store = Store(path=path)
    raw = _drive(store)
    blobs = {
        "events": json.dumps(store.events),
        "billing_log": json.dumps(getattr(store, "billing_log", [])),
        "accounts": json.dumps(store.accounts),
        "agents": json.dumps(store.agents),
        "state_file": open(path).read(),
    }
    jp = path + ".events.jsonl"
    if os.path.exists(jp):
        blobs["journal"] = open(jp).read()
    for name, blob in blobs.items():
        assert raw not in blob, f"raw key leaked into {name}"
        assert not SK.search(blob) or name in (), f"an sk_ pattern leaked into {name}"


def test_no_raw_key_in_logs_or_exceptions(hash_on, caplog):
    caplog.set_level(logging.DEBUG)
    store = Store(path=os.path.join(tempfile.mkdtemp(), "leak2.json"))
    raw = _drive(store)
    # nothing the app logged may contain the secret
    assert raw not in caplog.text
    # exception strings on the auth path must not echo the secret
    try:
        raise ValueError(creds.scope_error(store.get_agent(
            next(iter(store.agents))), "invoke"))
    except ValueError as e:
        assert raw not in str(e)


def test_static_repo_scan_no_raw_key_literals_outside_tests():
    """No committed raw sk_ literal in shipped code/config/docs (tests may use
    obviously-fake fixtures like sk_wrong / sk_deadbeef...)."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    out = subprocess.run(
        ["grep", "-rIn", "--include=*.py", "--include=*.json",
         "--include=*.md", "--include=*.yaml", "-E", r"sk_[0-9a-f]{24,}", root],
        capture_output=True, text=True).stdout
    offenders = [ln for ln in out.splitlines()
                 if "/tests/" not in ln and "test_" not in ln]
    assert not offenders, "raw-key literal in shipped files:\n" + "\n".join(offenders)


def test_production_kdf_cost_is_bounded():
    """A single verify at the production default must stay well under 100ms so
    per-request auth latency is acceptable on the pilot instance."""
    os.environ["GUILD_KDF_ITERS"] = "100000"
    import importlib
    importlib.reload(creds)
    h = creds.hash_key("sk_" + "a" * 48)
    t0 = time.perf_counter()
    assert creds.verify_key_hash("sk_" + "a" * 48, h)
    dt = (time.perf_counter() - t0) * 1000
    assert dt < 100, f"verify took {dt:.0f}ms at 100k iters"
    os.environ["GUILD_KDF_ITERS"] = "1000"; os.environ["GUILD_ALLOW_WEAK_KDF"]="1"
    importlib.reload(creds)


import pytest as _pytest


@_pytest.fixture(autouse=True)
def _force_json_backend(monkeypatch):
    """These tests validate JSON-backend internals (the .events.jsonl journal,
    the on-disk JSON state file, or the JSON migration source), so they pin the
    default JSON store regardless of an ambient GUILD_STORE=sqlite run."""
    monkeypatch.setenv("GUILD_STORE", "json")
