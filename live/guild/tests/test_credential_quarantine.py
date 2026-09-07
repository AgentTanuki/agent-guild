"""Contain historical disclosure without possessing or replaying its secrets."""
import pytest
from fastapi.testclient import TestClient

from app import credentials as creds
from app.main import app, store
from app.store import Store, UnknownAccount
from app.swarm.gateway import derive_actor


@pytest.fixture(params=["0", "1"])
def key_mode(request, monkeypatch):
    monkeypatch.setenv("GUILD_HASH_KEYS", request.param)
    monkeypatch.setenv("GUILD_ALLOW_WEAK_KDF", "1")
    monkeypatch.setenv("GUILD_KDF_ITERS", "1000")
    return request.param


def quarantine(monkeypatch, raw):
    monkeypatch.setattr(creds, "COMPROMISED_KEY_IDS",
                        creds.COMPROMISED_KEY_IDS | {creds.key_id_of(raw)})


def test_incident_policy_contains_only_public_identifiers():
    assert creds.QUARANTINE_VERSION == "credential-quarantine-2026-09-07"
    assert creds.COMPROMISED_KEY_IDS == {
        "285f0f76f02d6e7f14e7510b45208871",
        "8ecca85351bc2652a98eb541a75f6007",
    }
    assert all(len(k) == 32 and int(k, 16) >= 0
               for k in creds.COMPROMISED_KEY_IDS)


def test_quarantine_denies_auth_billing_and_member_tier(
        key_mode, monkeypatch, tmp_path):
    s = Store(path=str(tmp_path / "guild.json"))
    a = s.register_agent("Synthetic quarantine", ["test"], {})
    unaffected = s.register_agent("Unaffected", ["test"], {})
    raw = a["api_key"]
    account_key = s.resolve_billing_key(raw)
    balance = s.accounts[account_key]["balance"]
    quarantine(monkeypatch, raw)

    assert not creds.verify_agent_key(s.get_agent(a["id"]), raw)
    assert s.agent_for_presented_key(raw) is None
    assert s.resolve_billing_key(raw) is None
    assert s.get_account(raw) is None
    with pytest.raises(UnknownAccount):
        s.charge(raw, 1, "quarantine_test")
    assert s.accounts[account_key]["balance"] == balance
    _, member = derive_actor(raw, "192.0.2.1", "test", store=s)
    assert member is False

    assert creds.verify_agent_key(s.get_agent(unaffected["id"]),
                                  unaffected["api_key"])
    assert s.get_account(unaffected["api_key"]) is not None
    trial = s.grant_trial(10)
    assert s.get_account(trial["key"]) is not None


def test_quarantine_survives_restore_and_allows_admin_reissue(
        key_mode, monkeypatch, tmp_path):
    path = str(tmp_path / "guild.json")
    s = Store(path=path)
    a = s.register_agent("Synthetic restore", ["test"], {})
    raw = a["api_key"]
    balance = s.get_account(raw)["balance"]
    quarantine(monkeypatch, raw)
    restored = Store(path=path)
    assert restored.agent_for_presented_key(raw) is None
    assert restored.get_account(raw) is None
    # Only the server/admin rotation path is exercised here; a caller cannot
    # authorise its own rotation with the quarantined bearer.
    new = restored.rotate_api_key(a["id"])["api_key"]
    assert restored.agent_for_presented_key(new)["id"] == a["id"]
    assert restored.get_account(new)["balance"] == balance
    assert restored.get_account(raw) is None


def test_quarantined_public_id_cannot_authenticate_legacy_account(
        key_mode, tmp_path):
    s = Store(path=str(tmp_path / "guild.json"))
    kid = sorted(creds.COMPROMISED_KEY_IDS)[0]
    s._new_account(key=kid)  # synthetic malformed/restored legacy account
    assert s.resolve_billing_key(kid) is None


def test_http_cannot_rotate_mutate_or_read_account_with_quarantined_key(
        key_mode, monkeypatch):
    client = TestClient(app)
    a = store.register_agent("Synthetic HTTP quarantine", ["test"], {})
    quarantine(monkeypatch, a["api_key"])
    headers = {"X-API-Key": a["api_key"]}
    assert client.post(f"/agents/{a['id']}/key/rotate",
                       headers=headers).status_code == 401
    assert client.post(f"/agents/{a['id']}/endpoint", headers=headers,
                       json={"endpoint": "https://example.com/a2a"}
                       ).status_code == 401
    assert client.get("/billing/account", headers=headers).status_code in (401, 404)


def test_health_reports_aggregate_policy_without_identifiers():
    result = TestClient(app).get("/health").json()["credential_quarantine"]
    assert result == {"version": creds.QUARANTINE_VERSION, "blocked_key_ids": 2}
    assert not any(k in str(result) for k in creds.COMPROMISED_KEY_IDS)
