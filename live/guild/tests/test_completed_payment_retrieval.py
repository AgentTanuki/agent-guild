"""A paid result outlives the authorization window used to purchase it.

Synthetic mainnet receipts and a fake facilitator only; no network or funds.
Exercise actual HTTP output and durable JSON/SQLite reloads, with and without
the optional payment identifier.
"""
import copy

import pytest
from fastapi.testclient import TestClient

from app import main, payments, x402
from tests.test_payment_identifier import _pid, _with_pid
from tests.test_payment_recovery_no_identifier import (
    _anchor, _confirm_ok, fac, mainnet_store,
)
from tests.test_x402_v2 import make_payload, sig_header


def unavailable(*args, **kwargs):
    raise AssertionError("retrieving a completed purchase needs no new work")


@pytest.mark.parametrize("explicit_identifier", [False, True])
def test_shared_gateway_retrieves_expired_completed_purchase_without_facilitator_config(
        mainnet_store, fac, monkeypatch, explicit_identifier):
    mainnet_store()
    _anchor(monkeypatch)
    _confirm_ok(monkeypatch)
    preq = payments.search_request("gateway-recovery")
    payload = make_payload(preq, cost=preq.cost)
    if explicit_identifier:
        payload = _with_pid(payload, _pid())
    settled = payments.settle_x402(payload, preq)
    original = b'{"result":"the original paid answer"}'
    receipt = settled.finalize(original)["header"]
    mainnet_store()
    monkeypatch.setattr(x402.time, "time", lambda: float(
        payload.payload["authorization"]["validBefore"]) + 3600)
    monkeypatch.setattr(x402, "_facilitator", unavailable)
    monkeypatch.setattr(x402, "config_errors", lambda: ["facilitator unavailable"])
    with pytest.raises(payments.CachedPaidResult) as cached:
        payments.settle_x402(payload, preq)
    assert cached.value.result_bytes == original
    assert cached.value.settle_header == receipt
    assert len(fac.settle_calls) == 1
    with pytest.raises(x402.PaymentBindingError) as denied:
        payments.settle_x402(payload, preq, method="POST")
    assert denied.value.reason == "method_mismatch"


@pytest.mark.parametrize("path", ["check", "search"])
@pytest.mark.parametrize("explicit_identifier", [False, True])
def test_completed_http_purchase_survives_expiry_restart_and_provider_outage(
        mainnet_store, fac, monkeypatch, path, explicit_identifier):
    store = mainnet_store()
    monkeypatch.setattr(main, "store", store)
    _anchor(monkeypatch)
    _confirm_ok(monkeypatch)
    preq = (payments.check_request("paid-recovery") if path == "check"
            else payments.search_request("paid-recovery"))
    payload = make_payload(preq, cost=preq.cost)
    if explicit_identifier:
        payload = _with_pid(payload, _pid())
    headers = {"PAYMENT-SIGNATURE": sig_header(payload)}
    store.register_agent("Recovery supplier", ["paid-recovery", "another-task"], {})
    url = f"/{path}?capability=paid-recovery"
    with TestClient(main.app) as client:
        paid = client.get(url, headers=headers)
        assert paid.status_code == 200, paid.text
        assert len(fac.settle_calls) == 1
        original_receipt = paid.headers["PAYMENT-RESPONSE"]

        # The buyer lost this response, then restarts after the payment expired.
        restored = mainnet_store()
        monkeypatch.setattr(main, "store", restored)
        monkeypatch.setattr(x402.time, "time", lambda: float(
            payload.payload["authorization"]["validBefore"]) + 3600)
        monkeypatch.setattr(x402, "_facilitator", unavailable)
        original_check = restored.check
        original_reputation = restored.reputation
        monkeypatch.setattr(restored, "check", unavailable)
        monkeypatch.setattr(restored, "reputation", unavailable)
        retry = client.get(url, headers=headers)
        assert retry.status_code == 200, retry.text
        assert retry.content == paid.content
        assert retry.headers["PAYMENT-RESPONSE"] == original_receipt
        assert retry.headers["X-Guild-Payment-Idempotent-Replay"] == "true"
        assert len(fac.settle_calls) == 1

        # Possessing a public payment identifier must not expose the result.
        monkeypatch.setattr(restored, "check", original_check)
        monkeypatch.setattr(restored, "reputation", original_reputation)
        for change in ("signature", "payer", "query"):
            changed = copy.deepcopy(payload)
            target = url
            if change == "signature":
                changed.payload["signature"] = "0x" + "cd" * 65
            elif change == "payer":
                changed.payload["authorization"]["from"] = "0x" + "33" * 20
            else:
                target = f"/{path}?capability=another-task"
            denied = client.get(target, headers={
                "PAYMENT-SIGNATURE": sig_header(changed)})
            assert denied.status_code in (402, 409), denied.text
            assert "X-Guild-Payment-Idempotent-Replay" not in denied.headers
        assert len(fac.settle_calls) == 1


def test_expired_unpaid_authorization_cannot_create_a_recovery_record(
        mainnet_store, fac, monkeypatch):
    store = mainnet_store()
    _anchor(monkeypatch)
    preq = payments.search_request("never-paid")
    payload = _with_pid(make_payload(preq, valid_before=1), _pid())
    with pytest.raises(x402.PaymentBindingError) as exc:
        payments.settle_x402(payload, preq)
    assert exc.value.reason == "authorization_expired"
    assert not store.x402_payment_ids
    assert fac.verify_calls == fac.settle_calls == []
