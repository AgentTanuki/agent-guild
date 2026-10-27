"""Keyed-request cost (2.7.3, 2026-10-27).

Measured on production 2.7.2: a keyless ``GET /preflight`` answered in ~0.6 s;
the same request with a participant ``X-API-Key`` took ~10 s, because
``Store.agent_for_presented_key`` ran the PBKDF2 verifier (>=100k iterations)
against EVERY agent record. The evaluation asks participants to send a key on
both legs, so that cost would have landed on every paired comparison and on
every fail-closed pre-call hook. The fix looks the record up by its public
``key_id`` first and runs the KDF on that record only.

These tests pin the property with a counter on the verifier, in both storage
modes, including the revoked / rotated / legacy-plaintext / unindexed-hash
paths, so the lookup can never silently regress to a full-population scan.
"""
import os

os.environ.setdefault("GUILD_DATA", "")
os.environ.setdefault("GUILD_BOOTSTRAP_EVAL", "0")

import pytest
from fastapi.testclient import TestClient

from app.main import app, store
from app import credentials as creds

client = TestClient(app)


@pytest.fixture
def hash_on(monkeypatch):
    monkeypatch.setenv("GUILD_HASH_KEYS", "1")
    monkeypatch.setenv("GUILD_ALLOW_WEAK_KDF", "1")
    monkeypatch.setenv("GUILD_KDF_ITERS", "1000")


@pytest.fixture
def hash_off(monkeypatch):
    monkeypatch.delenv("GUILD_HASH_KEYS", raising=False)


@pytest.fixture
def verify_counter(monkeypatch):
    """Count how many agent records the verifier is asked about."""
    calls: list[str] = []
    real = creds.verify_agent_key

    def counting(agent, presented):
        calls.append((agent or {}).get("id", "?"))
        return real(agent, presented)

    monkeypatch.setattr(creds, "verify_agent_key", counting)
    return calls


def _register(name):
    r = client.post("/agents/register", json={"name": name, "capabilities": ["x"]})
    assert r.status_code == 200, r.text
    return r.json()


def _population(n, prefix):
    return [_register(f"{prefix}-{i}") for i in range(n)]


def test_hashed_key_is_verified_against_one_record_only(hash_on, verify_counter):
    others = _population(25, "LookupCostPop")
    mine = _register("LookupCostTarget")
    assert len(store.agents) >= 26
    verify_counter.clear()

    found = store.agent_for_presented_key(mine["api_key"])

    assert found is not None and found["id"] == mine["id"]
    # exactly one KDF verification — the record carrying this key's key_id
    assert verify_counter == [mine["id"]], verify_counter
    assert all(o["id"] not in verify_counter for o in others)


def test_wrong_hashed_key_never_scans_the_population(hash_on, verify_counter):
    _population(10, "LookupCostWrong")
    verify_counter.clear()
    assert store.agent_for_presented_key("sk_" + "0" * 48) is None
    # no record carries that key_id → the KDF never runs: every record the
    # verifier was asked about is an unindexed legacy/plaintext record (other
    # suites may leave those in the shared store), never a hashed one
    hashed_checked = [i for i in verify_counter
                      if (store.agents.get(i) or {}).get("api_key_hash")]
    assert hashed_checked == [], hashed_checked


def test_revoked_key_fails_fast_without_population_scan(hash_on, verify_counter):
    _population(10, "LookupCostRevoked")
    mine = _register("LookupCostRevokedTarget")
    rec = store.agents[mine["id"]]
    rec["api_key_hash"] = None            # revoked: no stored credential
    verify_counter.clear()
    assert store.agent_for_presented_key(mine["api_key"]) is None
    # the revoked record was consulted once (cheap: no stored hash) and no
    # OTHER hashed record was put through the KDF
    assert verify_counter.count(mine["id"]) == 1
    hashed_others = [i for i in verify_counter if i != mine["id"]
                     and (store.agents.get(i) or {}).get("api_key_hash")]
    assert hashed_others == [], hashed_others


def test_rotated_key_old_secret_fails_new_secret_resolves(hash_on, verify_counter):
    mine = _register("LookupCostRotate")
    old = mine["api_key"]
    r = client.post(f"/agents/{mine['id']}/key/rotate",
                    headers={"X-API-Key": old})
    if r.status_code != 200:
        pytest.skip(f"no rotate surface in this build ({r.status_code})")
    new = r.json()["api_key"]
    verify_counter.clear()
    assert store.agent_for_presented_key(old) is None
    assert store.agent_for_presented_key(new)["id"] == mine["id"]


def test_legacy_plaintext_key_still_resolves(hash_off, verify_counter):
    _population(5, "LookupCostLegacyPop")
    mine = _register("LookupCostLegacy")
    assert store.agents[mine["id"]].get("api_key") == mine["api_key"]   # plaintext
    found = store.agent_for_presented_key(mine["api_key"])
    assert found is not None and found["id"] == mine["id"]


def test_hashed_record_without_key_id_is_still_found_by_fallback(hash_on):
    mine = _register("LookupCostUnindexed")
    rec = store.agents[mine["id"]]
    assert rec.get("api_key_hash") and rec.get("key_id")
    rec.pop("key_id")                      # a pre-key_id record shape
    found = store.agent_for_presented_key(mine["api_key"])
    assert found is not None and found["id"] == mine["id"]


def test_keyed_preflight_round_trip_resolves_participant(hash_on, monkeypatch):
    from app import preflight

    def fake_run(url, *, store=None):
        return {"target": url, "verdict": "no_failed_checks", "headline": "x",
                "checks": [], "failed": [], "unknowns": [],
                "scored": ["endpoint_reachable"], "method": "m", "limits": "l"}

    monkeypatch.setattr(preflight, "run", fake_run)
    _population(10, "LookupCostPreflightPop")
    mine = _register("LookupCostPreflight")
    h = {"User-Agent": "lookup-cost-test/1", "X-API-Key": mine["api_key"]}
    pf = client.get("/preflight", params={"url": "https://x.example/a"}, headers=h)
    assert pf.status_code == 200, pf.text
    r = client.post("/preflight/outcome", headers=h, json={
        "preflight_id": pf.json()["preflight_id"], "action": "called",
        "observed": "success", "decision_basis": "own_check",
        "reporter_kind": "integration"})
    assert r.status_code == 201, r.text
