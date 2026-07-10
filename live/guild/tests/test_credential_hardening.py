"""Credential hardening (2026-07-10, branch credential-hardening).

Covers both modes of GUILD_HASH_KEYS:

  * OFF (default) — legacy behavior byte-for-byte: plaintext api_key on the
    record, accounts/events keyed by the raw key.
  * ON — sha256-at-rest, raw key shown once at issuance, public key_id in
    accounts + event actor keys, migration of pre-existing plaintext keys,
    scopes, rotation/revocation, and the no-secret-in-the-journal guarantee.

Design: docs/discovery-swarm/CREDENTIALS_DESIGN.md
"""
import json
import os
import tempfile

os.environ.setdefault("GUILD_DATA", "")
os.environ.setdefault("GUILD_BOOTSTRAP_EVAL", "0")

import pytest
from fastapi.testclient import TestClient

from app.main import app, store
from app import credentials as creds
from app.store import Store

client = TestClient(app)


@pytest.fixture
def hash_on(monkeypatch):
    monkeypatch.setenv("GUILD_HASH_KEYS", "1")


@pytest.fixture
def hash_off(monkeypatch):
    monkeypatch.delenv("GUILD_HASH_KEYS", raising=False)


def _register(name="CredHard"):
    r = client.post("/agents/register", json={"name": name, "capabilities": ["x"]})
    assert r.status_code == 200, r.text
    return r.json()


def _auth_probe(agent_id, key):
    """An authenticated write that exercises _require_key."""
    return client.post(f"/agents/{agent_id}/endpoint",
                       headers={"X-API-Key": key} if key else {},
                       json={"endpoint": "https://example.com/a2a"})


# --- mode OFF: legacy behavior unchanged -------------------------------------

def test_off_mode_stores_plaintext_and_authenticates(hash_off):
    a = _register("LegacyPlain")
    rec = store.agents[a["id"]]
    assert rec["api_key"] == a["api_key"]          # plaintext at rest (legacy)
    assert "api_key_hash" not in rec
    assert a["api_key"] in store.accounts           # account keyed by raw key
    assert _auth_probe(a["id"], a["api_key"]).status_code == 200
    assert _auth_probe(a["id"], "sk_wrong").status_code == 401


# --- mode ON: hash-at-rest, key shown once, key_id everywhere ---------------

def test_on_mode_hashes_at_rest_and_still_authenticates(hash_on):
    a = _register("HashedAtRest")
    raw = a["api_key"]
    assert raw and raw.startswith("sk_")            # shown once at issuance
    rec = store.agents[a["id"]]
    assert rec.get("api_key") is None               # never stored raw
    assert rec["api_key_hash"] == creds.hash_key(raw)
    assert rec["key_id"] == creds.hash_key(raw)[:12]
    assert rec["scopes"] == creds.DEFAULT_SCOPES    # default: all scopes
    assert rec["credential_class"] in ("first_party", "external")
    # account keyed by the public key_id, not the secret
    assert rec["key_id"] in store.accounts
    assert raw not in store.accounts
    # the raw key still authenticates (constant-time hash compare)
    assert _auth_probe(a["id"], raw).status_code == 200
    assert _auth_probe(a["id"], "sk_wrong").status_code == 401
    # the PUBLIC key_id must never work as a credential
    assert _auth_probe(a["id"], rec["key_id"]).status_code == 401
    assert client.get("/billing/account",
                      headers={"X-API-Key": rec["key_id"]}).status_code in (401, 404)


def test_on_mode_raw_key_absent_from_events_journal(hash_on):
    a = _register("NoSecretInEvents")
    raw = a["api_key"]
    # generate events across surfaces with the raw key presented
    client.get(f"/agents/{a['id']}/reputation", headers={"X-API-Key": raw})
    client.post("/attestations", headers={"X-API-Key": raw},
                json={"issuer_id": a["id"], "subject_id": a["id"],
                      "capability": "x", "rating": 1.0})  # 400 (self) — still logged
    dump = json.dumps(store.events)
    assert raw not in dump
    # attribution continuity: the register event is keyed by the key_id
    kid = store.agents[a["id"]]["key_id"]
    assert any(e.get("key") == kid and e.get("type") == "register"
               for e in store.events)
    # audit trail: issuance is an explicit event carrying only the key_id
    assert any(e.get("type") == "api_key_issued" and e.get("key_id") == kid
               for e in store.events)


def test_on_mode_rotation_invalidates_old_hash(hash_on):
    a = _register("RotateHashed")
    old = a["api_key"]
    old_hash = store.agents[a["id"]]["api_key_hash"]
    r = client.post(f"/agents/{a['id']}/key/rotate", headers={"X-API-Key": old})
    assert r.status_code == 200, r.text
    new = r.json()["api_key"]
    assert new != old and new.startswith("sk_")
    rec = store.agents[a["id"]]
    assert rec["api_key_hash"] == creds.hash_key(new) != old_hash
    assert rec.get("api_key") is None
    assert _auth_probe(a["id"], old).status_code == 401   # old hash gone
    assert _auth_probe(a["id"], new).status_code == 200
    # account followed the credential to the new key_id
    assert rec["key_id"] in store.accounts
    assert creds.key_id_of(old) not in store.accounts
    # audit event, key_id only
    assert any(e.get("type") == "api_key_rotated"
               and e.get("key_id") == rec["key_id"] for e in store.events)
    assert new not in json.dumps(store.events)


def test_on_mode_revoked_key_fails(hash_on):
    a = _register("RevokeHashed")
    raw = a["api_key"]
    r = client.post(f"/agents/{a['id']}/key/revoke", headers={"X-API-Key": raw})
    assert r.status_code == 200
    assert _auth_probe(a["id"], raw).status_code == 401
    rec = store.agents[a["id"]]
    assert rec.get("api_key") is None and rec.get("api_key_hash") is None
    assert any(e.get("type") == "api_key_revoked" for e in store.events)


def test_expired_key_fails():
    s = Store(path="")
    a = s.register_agent("Expiry", ["x"], {})
    out = s.rotate_api_key(a["id"], expires_in_days=-1)   # already expired
    rec = s.get_agent(a["id"])
    assert not creds.verify_agent_key(rec, out["api_key"])
    out2 = s.rotate_api_key(a["id"])                       # no expiry
    assert creds.verify_agent_key(s.get_agent(a["id"]), out2["api_key"])


# --- migration of pre-existing plaintext keys --------------------------------

def test_migration_hashes_plaintext_in_place_on_first_load(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "mig.json")
    monkeypatch.delenv("GUILD_HASH_KEYS", raising=False)
    s1 = Store(path=path)
    a = s1.register_agent("PreExisting", ["x"], {})
    raw = a["api_key"]
    assert s1.agents[a["id"]]["api_key"] == raw            # plaintext today
    assert raw in open(path).read()                        # ...and on disk

    monkeypatch.setenv("GUILD_HASH_KEYS", "1")
    s2 = Store(path=path)                                  # migration on load
    rec = s2.get_agent(a["id"])
    kid = creds.key_id_of(raw)
    assert rec["api_key"] is None
    assert rec["api_key_hash"] == creds.hash_key(raw)
    assert rec["key_id"] == kid
    assert rec["scopes"] == creds.DEFAULT_SCOPES
    # account re-keyed raw -> key_id; balances intact
    assert kid in s2.accounts and raw not in s2.accounts
    assert s2.accounts[kid]["owner_agent_id"] == a["id"]
    # historical event actor keys rewritten
    assert all(e.get("key") != raw and e.get("actor") != raw for e in s2.events)
    assert any(e.get("key") == kid and e.get("type") == "register"
               for e in s2.events)
    assert any(e.get("type") == "api_keys_migrated" for e in s2.events)
    # the old raw key STILL authenticates (it hashes to the stored digest)
    assert creds.verify_agent_key(rec, raw)
    # and no secret remains anywhere on disk (main file or journal)
    on_disk = open(path).read()
    if os.path.exists(path + ".events.jsonl"):
        on_disk += open(path + ".events.jsonl").read()
    assert raw not in on_disk
    # idempotent: a third load changes nothing
    s3 = Store(path=path)
    assert s3.get_agent(a["id"])["api_key_hash"] == creds.hash_key(raw)


def test_off_mode_never_migrates(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "nomig.json")
    monkeypatch.delenv("GUILD_HASH_KEYS", raising=False)
    s1 = Store(path=path)
    a = s1.register_agent("StaysPlain", ["x"], {})
    s2 = Store(path=path)  # reload with the flag still off
    assert s2.get_agent(a["id"])["api_key"] == a["api_key"]  # unchanged


# --- scopes -------------------------------------------------------------------

def _narrow(agent_id, scopes):
    store.agents[agent_id]["scopes"] = scopes


def test_scope_denial_is_machine_readable_403_attest(hash_off):
    a = _register("ScopedIssuer")
    b = _register("ScopedSubject")
    _narrow(a["id"], ["read"])
    r = client.post("/attestations", headers={"X-API-Key": a["api_key"]},
                    json={"issuer_id": a["id"], "subject_id": b["id"],
                          "capability": "x", "rating": 0.9})
    assert r.status_code == 403
    body = r.json()["detail"]
    assert body["error"] == "missing_scope"
    assert body["required_scope"] == "attest"
    assert body["have_scopes"] == ["read"]
    assert body["agent_id"] == a["id"]
    # restoring the scope restores the capability
    _narrow(a["id"], ["read", "attest"])
    assert client.post("/attestations", headers={"X-API-Key": a["api_key"]},
                       json={"issuer_id": a["id"], "subject_id": b["id"],
                             "capability": "x", "rating": 0.9}).status_code == 200


def test_scope_denial_escrow(hash_on):
    a = _register("ScopedPayer")
    b = _register("ScopedWorker")
    _narrow(a["id"], ["read", "invoke"])
    r = client.post("/escrow", headers={"X-API-Key": a["api_key"]},
                    json={"worker_id": b["id"], "amount": 5})
    assert r.status_code == 403
    assert r.json()["detail"]["required_scope"] == "escrow"


def test_scope_denial_member_invoke(hash_on):
    from app.swarm.capabilities import CAPABILITIES
    cap_id = next(iter(CAPABILITIES))
    a = _register("ScopedInvoker")
    _narrow(a["id"], ["read"])
    r = client.post(f"/invoke/{cap_id}", headers={"X-API-Key": a["api_key"]},
                    json={})
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["denied"] == "missing_scope"
    assert body["required_scope"] == "invoke"
    # a guest (no key at all) is still welcome on the same capability
    g = client.post(f"/invoke/{cap_id}", json={})
    assert g.status_code != 403


def test_scope_denial_key_rotate_requires_admin_scope(hash_off):
    a = _register("ScopedRotator")
    _narrow(a["id"], ["read", "invoke", "attest", "escrow"])  # everything but admin
    r = client.post(f"/agents/{a['id']}/key/rotate",
                    headers={"X-API-Key": a["api_key"]})
    assert r.status_code == 403
    assert r.json()["detail"]["required_scope"] == "admin"
    r = client.post(f"/agents/{a['id']}/key/revoke",
                    headers={"X-API-Key": a["api_key"]})
    assert r.status_code == 403
    assert r.json()["detail"]["required_scope"] == "admin"


def test_absent_scopes_field_means_all_scopes(hash_off):
    a = _register("Unscoped")
    store.agents[a["id"]].pop("scopes", None)   # pre-hardening record shape
    b = _register("UnscopedSubject")
    assert client.post("/attestations", headers={"X-API-Key": a["api_key"]},
                       json={"issuer_id": a["id"], "subject_id": b["id"],
                             "capability": "x", "rating": 0.8}).status_code == 200
