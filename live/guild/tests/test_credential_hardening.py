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
    monkeypatch.setenv("GUILD_ALLOW_WEAK_KDF", "1")
    monkeypatch.setenv("GUILD_KDF_ITERS", "1000")   # fast for tests


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
    # salted PBKDF2 verifier: self-describing, non-deterministic (fresh salt),
    # so it is NOT equal to a re-hash — it must VERIFY the raw key instead.
    assert rec["api_key_hash"].startswith("pbkdf2_sha256$")
    assert creds.verify_key_hash(raw, rec["api_key_hash"])
    assert not creds.verify_key_hash("sk_wrong", rec["api_key_hash"])
    assert rec["key_id"] == creds.key_id_of(raw)    # deterministic identifier
    assert rec["scopes"] == list(creds.DEFAULT_ISSUE_SCOPES)  # least privilege
    assert "admin" not in rec["scopes"]             # never granted by default
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
    assert creds.verify_key_hash(new, rec["api_key_hash"])
    assert not creds.verify_key_hash(old, rec["api_key_hash"])  # old key dead
    assert rec["api_key_hash"] != old_hash                       # verifier rotated
    assert rec.get("api_key") is None
    assert _auth_probe(a["id"], old).status_code == 401   # old credential gone
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
    assert rec["api_key_hash"].startswith("pbkdf2_sha256$")
    assert creds.verify_key_hash(raw, rec["api_key_hash"])
    assert rec["key_id"] == kid
    assert rec["scopes"] == list(creds.DEFAULT_ISSUE_SCOPES)  # least privilege
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
    # idempotent: a third load re-migrates nothing (already hashed) and the
    # stored verifier still authenticates the original raw key. (The verifier
    # is salted, so it is NOT byte-equal to a fresh hash — verify, don't compare.)
    hash_before = s2.get_agent(a["id"])["api_key_hash"]
    s3 = Store(path=path)
    rec3 = s3.get_agent(a["id"])
    assert rec3["api_key_hash"] == hash_before          # untouched on reload
    assert creds.verify_key_hash(raw, rec3["api_key_hash"])


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


def test_self_rotate_revoke_do_not_require_admin_scope(hash_off):
    """Corrected from the branch's first version: authenticating with the
    agent's OWN current key proves ownership, so rotating/retiring your own
    credential is a least-privilege SELF-action and must NOT require the
    operator-only `admin` scope (a self-registered key never carries it).
    Autonomous credential lifecycle would be impossible otherwise."""
    a = _register("SelfRotator")
    _narrow(a["id"], ["read", "invoke", "attest", "escrow"])  # least privilege, no admin
    r = client.post(f"/agents/{a['id']}/key/rotate",
                    headers={"X-API-Key": a["api_key"]})
    assert r.status_code == 200, r.text
    new = r.json()["api_key"]
    r = client.post(f"/agents/{a['id']}/key/revoke",
                    headers={"X-API-Key": new})
    assert r.status_code == 200, r.text


def test_legacy_absent_scopes_is_least_privilege_not_admin(hash_off):
    """Legacy policy: a record with no `scopes` field gets the least-privilege
    member set (read/invoke/attest/escrow) — NOT admin, NOT all."""
    a = _register("Legacy")
    store.agents[a["id"]].pop("scopes", None)   # pre-hardening record shape
    b = _register("LegacySubject")
    # attest (a member scope) works
    assert client.post("/attestations", headers={"X-API-Key": a["api_key"]},
                       json={"issuer_id": a["id"], "subject_id": b["id"],
                             "capability": "x", "rating": 0.8}).status_code == 200
    # but the record does NOT hold admin
    assert not creds.has_scope(store.agents[a["id"]], "admin")
    assert set(creds.scopes_of(store.agents[a["id"]])) == set(creds.LEGACY_SCOPES)


def test_legacy_credential_use_is_audited_once_and_listed(hash_off):
    a = _register("LegacyAudited")
    store.agents[a["id"]].pop("scopes", None)
    # operator view lists it before first use
    listed = store.legacy_scope_credentials()
    assert any(c["agent_id"] == a["id"] for c in listed["credentials"])
    # drive two authenticated (write) calls; the audit event fires exactly once
    b = _register("LegacyAuditSubj")
    for _ in range(2):
        client.post("/attestations", headers={"X-API-Key": a["api_key"]},
                    json={"issuer_id": a["id"], "subject_id": b["id"],
                          "capability": "x", "rating": 0.7})
    evs = [e for e in store.events
           if e.get("type") == "legacy_credential_used" and e.get("agent_id") == a["id"]]
    assert len(evs) == 1
    assert evs[0].get("effective_scopes") == list(creds.LEGACY_SCOPES)


def test_legacy_rotation_writes_explicit_modern_scopes(hash_off):
    a = _register("LegacyRotate")
    store.agents[a["id"]].pop("scopes", None)
    assert creds.is_legacy_scope(store.agents[a["id"]])
    r = client.post(f"/agents/{a['id']}/key/rotate", headers={"X-API-Key": a["api_key"]})
    assert r.status_code == 200, r.text
    rec = store.agents[a["id"]]
    assert not creds.is_legacy_scope(rec)                       # now explicit
    assert rec["scopes"] == list(creds.DEFAULT_ISSUE_SCOPES)


def test_scope_matrix(hash_off):
    a = _register("Matrix")
    rec = store.agents[a["id"]]
    # missing scopes -> legacy least-privilege
    rec.pop("scopes", None)
    assert set(creds.scopes_of(rec)) == set(creds.LEGACY_SCOPES)
    # empty scopes -> nothing
    rec["scopes"] = []
    assert creds.scopes_of(rec) == []
    assert not creds.has_scope(rec, "invoke")
    # unknown scope values -> dropped (fail closed per value)
    rec["scopes"] = ["invoke", "superuser", "root"]
    assert creds.scopes_of(rec) == ["invoke"]
    assert not creds.has_scope(rec, "superuser")
    # malformed scopes field (not a list) -> nothing
    rec["scopes"] = "invoke"
    assert creds.scopes_of(rec) == []
    # explicit least-privilege
    rec["scopes"] = list(creds.DEFAULT_ISSUE_SCOPES)
    assert creds.has_scope(rec, "escrow") and not creds.has_scope(rec, "admin")
    # explicit admin
    rec["scopes"] = ["admin"]
    assert creds.has_scope(rec, "admin") and not creds.has_scope(rec, "invoke")
    # unknown REQUIRED scope always fails closed
    assert not creds.has_scope(rec, "not_a_scope")


def test_scope_denial_is_audited(hash_off):
    a = _register("Denied")
    store.agents[a["id"]]["scopes"] = ["read"]   # no attest
    b = _register("DeniedSubject")
    r = client.post("/attestations", headers={"X-API-Key": a["api_key"]},
                    json={"issuer_id": a["id"], "subject_id": b["id"],
                          "capability": "x", "rating": 0.5})
    assert r.status_code == 403
    assert any(e.get("type") == "scope_denied" and e.get("required_scope") == "attest"
               and e.get("agent_id") == a["id"] for e in store.events)


import pytest as _pytest


@_pytest.fixture(autouse=True)
def _force_json_backend(monkeypatch):
    """These tests validate JSON-backend internals (the .events.jsonl journal,
    the on-disk JSON state file, or the JSON migration source), so they pin the
    default JSON store regardless of an ambient GUILD_STORE=sqlite run."""
    monkeypatch.setenv("GUILD_STORE", "json")
