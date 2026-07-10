"""Pilot A audit (2026-07-10): credential lifecycle + member-tier integrity.

1. A credential can be issued, tested, rotated and revoked machine-to-machine.
2. The guest gateway grants member tier only to REGISTERED keys — previously
   any non-empty X-API-Key string got the member budget and polluted
   member-keyed attribution.
"""
import os, tempfile

os.environ.setdefault("GUILD_DATA", os.path.join(tempfile.mkdtemp(), "g.json"))
os.environ["GUILD_ADMIN_TOKEN"] = "test-admin"

from fastapi.testclient import TestClient
from app.main import app, store

client = TestClient(app)


def _register(name="KeyLifecycle"):
    return client.post("/agents/register",
                       json={"name": name, "capabilities": ["x"]}).json()


def test_rotate_migrates_account_and_old_key_stops_working():
    a = _register()
    old = a["api_key"]
    r = client.post(f"/agents/{a['id']}/key/rotate", headers={"X-API-Key": old})
    assert r.status_code == 200, r.text
    new = r.json()["api_key"]
    assert new != old and new.startswith("sk_")
    # old key no longer authenticates; new one does
    bad = client.post(f"/agents/{a['id']}/endpoint", headers={"X-API-Key": old},
                      json={"endpoint": "https://example.com/a2a"})
    assert bad.status_code == 401
    good = client.post(f"/agents/{a['id']}/endpoint", headers={"X-API-Key": new},
                       json={"endpoint": "https://example.com/a2a"})
    assert good.status_code == 200
    # billing account followed the key (keyed by key_id under GUILD_HASH_KEYS=1)
    from app import credentials as creds
    new_acct = creds.key_id_of(new) if creds.hashing_enabled() else new
    old_acct = creds.key_id_of(old) if creds.hashing_enabled() else old
    assert new_acct in store.accounts and old_acct not in store.accounts


def test_revoke_stops_auth_and_admin_can_reissue():
    # ADMIN_TOKEN is captured at app-import time; when the full suite runs,
    # another test file may have imported the app first, so pin it here.
    import app.main as main_mod
    prev = main_mod.ADMIN_TOKEN
    main_mod.ADMIN_TOKEN = "test-admin"
    try:
        a = _register("Revokee")
        key = a["api_key"]
        r = client.post(f"/agents/{a['id']}/key/revoke", headers={"X-API-Key": key})
        assert r.status_code == 200
        # revoked key cannot authenticate anything, incl. another revoke/rotate
        assert client.post(f"/agents/{a['id']}/key/rotate",
                           headers={"X-API-Key": key}).status_code == 401
        # a missing key must never authenticate against a revoked (None) key
        assert client.post(f"/agents/{a['id']}/endpoint",
                           json={"endpoint": "https://example.com"}).status_code == 401
        # admin re-issues
        r = client.post(f"/agents/{a['id']}/key/rotate",
                        headers={"X-Admin-Token": "test-admin"})
        assert r.status_code == 200 and r.json()["api_key"].startswith("sk_")
    finally:
        main_mod.ADMIN_TOKEN = prev


def test_gateway_member_tier_requires_registered_key():
    from app.swarm.gateway import derive_actor
    from app import credentials as creds
    a = _register("GatewayMember")
    actor, member = derive_actor(a["api_key"], "1.2.3.4", "ua", store=store)
    # members are keyed by the raw key (legacy) or its public key_id (hashed)
    expected = (creds.key_id_of(a["api_key"]) if creds.hashing_enabled()
                else a["api_key"])
    assert member and actor == expected
    actor, member = derive_actor("sk_fake_nonsense", "1.2.3.4", "ua", store=store)
    assert not member and actor.startswith("swarm:badkey:")
    actor, member = derive_actor(None, "1.2.3.4", "ua", store=store)
    assert not member and actor.startswith("swarm:")


def test_gateway_invoke_downgrades_fake_key_to_guest():
    from app.swarm import gateway
    from app.swarm.router import ensure_built
    ensure_built()
    status, out = gateway.invoke(
        store, "json.canonicalize", {"value": {"a": 1}},
        x_api_key="sk_totally_fake", client_host="9.9.9.9", ua="test",
        first_party=True, base="http://test")
    assert status == 200
    assert out["rate"]["tier"] == "guest"
