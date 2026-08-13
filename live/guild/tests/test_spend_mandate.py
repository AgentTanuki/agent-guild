"""AGSM-1 ownership, replay safety, persistence and transport truthfulness."""
from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from app import callerproof, spendmandate, vc
from app.store import Store

NETWORK = "eip155:8453"
ASSET = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


def _create_body(maximum="50000", per="30000", count=3, window=3600,
                 cooldown=0):
    return {
        "network": NETWORK,
        "asset": ASSET,
        "caps": {
            "window_s": window,
            "max_atomic": maximum,
            "per_counterparty_atomic": per,
            "max_authorizations": count,
        },
        "new_payee_cooldown_s": cooldown,
        "expires_s": 86400,
    }


def _authorization(mandate_id, auth_id, payee, amount="20000"):
    return {
        "mandate_id": mandate_id,
        "authorization_id": auth_id,
        "payment": {
            "scheme": "exact", "network": NETWORK, "asset": ASSET,
            "amount": amount, "pay_to": payee,
            "resource": "https://seller.example/work/42",
        },
    }


def _owner(private_key: str) -> str:
    return callerproof.create_evm_proof(
        private_key, method="GET", resource="/owner", body=b"",
        nonce="owner-address-only")["payload"]["did"]


def _header(private_key: str, method: str, resource: str, body: bytes,
            nonce: str) -> str:
    env = callerproof.create_evm_proof(
        private_key, method=method, resource=resource, body=body, nonce=nonce)
    return base64.b64encode(json.dumps(env).encode()).decode()


def _subject(credential):
    assert vc.verify_credential(credential) is True
    return credential["credentialSubject"]


def test_signed_mandate_consumes_caps_and_blocks_every_replay():
    local = Store(path="")
    owner = _owner("0x" + "21" * 32)
    payee = Account.create().address
    at = datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc)
    made = spendmandate.create(
        local, _create_body(), caller_did=owner, now=at)
    assert made["contract"] == spendmandate.CONTRACT
    assert _subject(made["credential"])["mandate_digest"] == made["mandate_digest"]

    first = spendmandate.authorize_and_issue(
        local, _authorization(made["mandate_id"], "payment-auth-0001", payee),
        caller_did=owner, now=at + timedelta(seconds=1))
    assert _subject(first)["authorized"] is True
    assert _subject(first)["spend_state"]["spent_after_atomic"] == "20000"

    replay = spendmandate.authorize_and_issue(
        local, _authorization(made["mandate_id"], "payment-auth-0001", payee),
        caller_did=owner, now=at + timedelta(seconds=2))
    assert _subject(replay)["authorized"] is False
    assert _subject(replay)["decision"] == "block"
    assert _subject(replay)["idempotent_replay"] is True
    assert "authorization_id_already_consumed" in _subject(replay)["failures"]
    assert local.spend_mandates[made["mandate_id"]]["authorization_count"] == 1

    cap = spendmandate.authorize_and_issue(
        local, _authorization(made["mandate_id"], "payment-auth-0002", payee),
        caller_did=owner, now=at + timedelta(seconds=3))
    assert _subject(cap)["authorized"] is False
    assert "mandate_counterparty_cap_exceeded" in _subject(cap)["failures"]

    intruder = _owner("0x" + "22" * 32)
    with pytest.raises(spendmandate.SpendMandateRefused, match="unknown mandate"):
        spendmandate.authorize_and_issue(
            local,
            _authorization(made["mandate_id"], "payment-auth-0003", payee),
            caller_did=intruder, now=at + timedelta(seconds=4))


def test_window_reset_keeps_old_ids_blocked_and_cooldown_tracks_each_new_payee():
    local = Store(path="")
    owner = _owner("0x" + "23" * 32)
    payee1, payee2, payee3 = (Account.create().address for _ in range(3))
    at = datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc)
    made = spendmandate.create(
        local, _create_body(maximum="60000", per="30000", window=60,
                            cooldown=20), caller_did=owner, now=at)
    first_req = _authorization(
        made["mandate_id"], "window-payment-01", payee1, "10000")
    assert _subject(spendmandate.authorize_and_issue(
        local, first_req, caller_did=owner,
        now=at + timedelta(seconds=1)))["authorized"]

    cooled = spendmandate.authorize_and_issue(
        local, _authorization(
            made["mandate_id"], "window-payment-02", payee2, "10000"),
        caller_did=owner, now=at + timedelta(seconds=10))
    assert "mandate_new_payee_cooldown_active" in _subject(cooled)["failures"]
    second = spendmandate.authorize_and_issue(
        local, _authorization(
            made["mandate_id"], "window-payment-03", payee2, "10000"),
        caller_did=owner, now=at + timedelta(seconds=21))
    assert _subject(second)["authorized"]
    third_too_soon = spendmandate.authorize_and_issue(
        local, _authorization(
            made["mandate_id"], "window-payment-04", payee3, "10000"),
        caller_did=owner, now=at + timedelta(seconds=30))
    assert "mandate_new_payee_cooldown_active" in _subject(
        third_too_soon)["failures"]

    reset = spendmandate.authorize_and_issue(
        local, _authorization(
            made["mandate_id"], "window-payment-05", payee1, "10000"),
        caller_did=owner, now=at + timedelta(seconds=61))
    assert _subject(reset)["authorized"]
    assert _subject(reset)["spend_state"]["window_reset"] is True
    old = spendmandate.authorize_and_issue(
        local, first_req, caller_did=owner, now=at + timedelta(seconds=62))
    assert _subject(old)["decision"] == "block"
    assert "authorization_id_already_consumed" in _subject(old)["failures"]


def test_sqlite_restart_and_parallel_authorization_do_not_overspend(
        tmp_path, monkeypatch):
    db = tmp_path / "mandates.sqlite3"
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    monkeypatch.setenv("GUILD_STORE_PATH", str(db))
    owner = _owner("0x" + "24" * 32)
    payee = Account.create().address
    local = Store(path="")
    made = spendmandate.create(
        local, _create_body(maximum="20000", per="20000", count=10),
        caller_did=owner)
    assert Store(path="").spend_mandates[made["mandate_id"]][
        "mandate_digest"] == made["mandate_digest"]

    def attempt(index):
        credential = spendmandate.authorize_and_issue(
            Store(path=""), _authorization(
                made["mandate_id"], f"parallel-payment-{index:02d}",
                payee, "15000"), caller_did=owner)
        return _subject(credential)["authorized"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [False, True]
    final = Store(path="").spend_mandates[made["mandate_id"]]
    assert final["spent_atomic"] == "15000"
    assert final["authorization_count"] == 1
    assert Store(path="").spend_mandate_metrics[made["mandate_id"]][
        "external_authorizations"] == 1


def test_http_free_authority_is_separate_from_paid_agpd_and_measurable(
        monkeypatch):
    from app import main

    local = Store(path="")
    monkeypatch.setattr(main, "store", local)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    private = "0x" + "25" * 32
    body = _create_body()
    raw = json.dumps(body, separators=(",", ":")).encode()
    ua = "node-fetch/3 external-wallet-fixture"
    with TestClient(main.app) as client:
        assert client.post("/mandates", content=raw, headers={
            "content-type": "application/json"}).status_code == 401
        made = client.post("/mandates", content=raw, headers={
            "content-type": "application/json", "user-agent": ua,
            callerproof.HTTP_HEADER: _header(
                private, "POST", "/mandates", raw, "create-http")})
        assert made.status_code == 200, made.text
        mandate_id = made.json()["mandate_id"]
        payee = Account.create().address

        # Ordinary AGPD remains paid; mandate fields are not a free bypass.
        ordinary = {"payment": _authorization(
            mandate_id, "unused-auth-00", payee)["payment"]}
        assert client.post("/wallet-binding/decision", json=ordinary).status_code == 402

        for number, amount in ((1, "20000"), (2, "5000")):
            request = _authorization(
                mandate_id, f"http-payment-auth-0{number}", payee, amount)
            request_raw = json.dumps(request, separators=(",", ":")).encode()
            issued = client.post("/mandates/authorize", content=request_raw,
                headers={
                    "content-type": "application/json", "user-agent": ua,
                    callerproof.HTTP_HEADER: _header(
                        private, "POST", "/mandates/authorize", request_raw,
                        f"authorize-http-{number}"),
                })
            assert issued.status_code == 200, issued.text
            assert issued.headers["x-guild-cost"] == "0"
            assert _subject(issued.json())["authorized"] is True
            assert "counterparty" not in _subject(issued.json())
            assert "risk" not in _subject(issued.json())

        signal = client.get("/funnel/spend-mandates").json()
        assert signal["signal_detected"] is True
        assert signal["successful_external_actors"] == 1
        assert signal["successful_mandates"] == 1

        assert client.post("/mandates/authorize", json=_authorization(
            mandate_id, "no-proof-auth-00", payee, "1")).status_code == 401
        revoke_path = f"/mandates/{mandate_id}/revoke"
        revoked = client.post(revoke_path, headers={
            callerproof.HTTP_HEADER: _header(
                private, "POST", revoke_path, b"", "revoke-http")})
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["status"] == "revoked"


def test_metric_is_durable_aggregate_only_and_first_party_heals_at_read_time(
        monkeypatch):
    local = Store(path="")
    owner = _owner("0x" + "26" * 32)
    payee = Account.create().address
    at = datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc)
    made = spendmandate.create(
        local, _create_body(), caller_did=owner, now=at)
    for number in (1, 2):
        assert _subject(spendmandate.authorize_and_issue(
            local, _authorization(
                made["mandate_id"], f"aggregate-auth-{number:02d}", payee,
                "1000"),
            caller_did=owner, now=at + timedelta(seconds=number)
        ))["authorized"] is True
    local.events.clear()  # measurement cannot depend on the truncating event log
    report = local.spend_mandate_falsification()
    assert report["signal_detected"] is True
    serialized = json.dumps(report).lower()
    assert made["mandate_id"].lower() not in serialized
    assert owner.lower() not in serialized
    assert payee.lower() not in serialized
    assert "evidence" not in report

    address = owner.rsplit(":", 1)[-1]
    monkeypatch.setenv("GUILD_X402_FIRST_PARTY_PAYERS", address)
    healed = local.spend_mandate_falsification()
    assert healed["signal_detected"] is False
    assert healed["eligible_external_mandates"] == 0


def test_sqlite_metric_secret_experiment_and_signal_survive_restart(
        tmp_path, monkeypatch):
    db = tmp_path / "metric-durability.sqlite3"
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    monkeypatch.setenv("GUILD_STORE_PATH", str(db))
    owner = _owner("0x" + "29" * 32)
    payee = Account.create().address
    at = datetime(2026, 8, 13, 2, 30, tzinfo=timezone.utc)
    local = Store(path="")
    made = spendmandate.create(
        local, _create_body(), caller_did=owner, now=at)
    secret = local.spend_mandate_metric_secret
    experiment = dict(local.spend_mandate_experiment)
    for number in (1, 2):
        assert _subject(spendmandate.authorize_and_issue(
            local, _authorization(
                made["mandate_id"], f"sqlite-metric-auth-{number:02d}",
                payee, "1000"),
            caller_did=owner, now=at + timedelta(seconds=number)
        ))["authorized"] is True

    reopened = Store(path="")
    assert reopened.spend_mandate_metric_secret == secret
    assert reopened.spend_mandate_experiment == experiment
    report = reopened.spend_mandate_falsification()
    assert report["signal_detected"] is True
    assert report["enrollment_open"] is True
    assert report["mandates_created_total"] == 1
    assert report["eligible_external_actors"] == 1
    assert report["successful_external_actors"] == 1


def test_first_party_creation_never_qualifies_and_window_closes():
    local = Store(path="")
    owner = _owner("0x" + "27" * 32)
    payee = Account.create().address
    at = datetime(2026, 8, 13, 3, 0, tzinfo=timezone.utc)
    made = spendmandate.create(
        local, _create_body(), caller_did=owner, now=at, first_party=True)
    for number in (1, 2):
        assert _subject(spendmandate.authorize_and_issue(
            local, _authorization(
                made["mandate_id"], f"first-party-auth-{number:02d}", payee,
                "1000"),
            caller_did=owner, now=at + timedelta(seconds=number),
            first_party=True,
        ))["authorized"] is True
    assert local.spend_mandate_falsification()["signal_detected"] is False
    with pytest.raises(spendmandate.SpendMandateRefused, match="window is closed"):
        spendmandate.authorize_and_issue(
            local, _authorization(
                made["mandate_id"], "late-window-auth-01", payee, "1000"),
            caller_did=owner,
            now=at + timedelta(days=spendmandate.EXPERIMENT_WINDOW_DAYS,
                               seconds=1))


def test_experiment_clock_is_persisted_outside_price_engine():
    local = Store(path="")
    at = datetime(2026, 8, 13, 4, 0, tzinfo=timezone.utc)
    rec = spendmandate.ensure_experiment(local, now=at)
    assert local.spend_mandate_experiment == rec
    assert local.experiments == {}


def test_json_multiworker_flag_in_argv_is_refused(monkeypatch):
    monkeypatch.setenv("GUILD_STORE", "json")
    local = Store(path="")
    owner = _owner("0x" + "28" * 32)
    monkeypatch.setattr(spendmandate.sys, "argv", [
        "uvicorn", "app.main:app", "--workers", "2"])
    with pytest.raises(spendmandate.SpendMandateRefused, match="requires SQLite"):
        spendmandate.create(local, _create_body(), caller_did=owner)
