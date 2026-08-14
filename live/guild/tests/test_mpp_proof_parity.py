"""Native-MPP parity for caller-proof-gated paid routes.

MPP and x402 share the same settlement path.  These tests ensure that native
MPP is also included in every pre-meter decision: proof nonces are consumed on
MPP retries, payment attempts are never misclassified as anonymous discovery,
and the MPP kill switch preserves the prior x402-only behaviour.  All payment
credentials here are deliberately invalid; settlement is spied and must stay
at zero.
"""
from __future__ import annotations

import base64
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import callerproof, crypto, payments, pricing, x402  # noqa: E402
import app.main as main  # noqa: E402


@pytest.fixture(autouse=True)
def _enforced(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_MPP_ENABLED", "1")
    monkeypatch.setenv(
        "GUILD_MPP_SECRET",
        "unit-test-secret-0123456789abcdef-0123456789abcdef")
    monkeypatch.setenv("GUILD_X402_NETWORK", "eip155:8453")
    monkeypatch.setenv(
        "GUILD_X402_ASSET",
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    pricing.load_runtime({})


@pytest.fixture()
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture()
def settle_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(
        payments, "settle_x402",
        lambda *args, **kwargs: calls.append((args, kwargs)))
    return calls


def _bad_mpp() -> str:
    """Recognisable native-MPP signal with no executable credential."""
    raw = json.dumps({"challenge": {}, "payload": {}}).encode()
    return "Payment " + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _identity():
    private, public = crypto.generate_keypair()
    return private, crypto.did_from_public_key(public)


def _decision_body(nonce: str):
    private, did = _identity()
    semantic = {"capability": "fact-check", "ttl_seconds": 3600}
    proof = callerproof.create_proof(
        private, did, method="POST", resource="/check/decision",
        body=callerproof.http_marketplace_body(semantic), nonce=nonce)
    return {"request": semantic, "caller_proof": proof}


PROOF_ROUTES = [
    "/check/decision",
    "/wallet-binding/decision",
    "/wallet-binding/protected-decision",
    "/wallet-binding/protected-decision/tiers/1000-usdc",
    "/envelopes/issue",
]


def test_native_mpp_retry_consumes_caller_proof_nonce(client, monkeypatch):
    """The production defect passed mark_nonce=False before MPP settlement."""
    seen = []
    real_verify = callerproof.verify_proof

    def spy(store, envelope, **kwargs):
        seen.append(kwargs.get("mark_nonce"))
        return real_verify(store, envelope, **kwargs)

    monkeypatch.setattr(callerproof, "verify_proof", spy)
    body = _decision_body("mpp-parity-consume")
    first = client.post(
        "/check/decision", json=body,
        headers={"Authorization": _bad_mpp()})
    assert first.status_code == 402, first.text
    assert seen == [True]

    # Even though the deliberately malformed payment could not settle, its
    # executing retry consumed the caller's proof exactly like x402 v2.  The
    # identical proof cannot be replayed into a later valid payment.
    replay = client.post(
        "/check/decision", json=body,
        headers={"Authorization": _bad_mpp()})
    assert replay.status_code == 401, replay.text


def test_unpaid_quote_preserves_caller_proof_nonce(client, monkeypatch):
    seen = []
    real_verify = callerproof.verify_proof

    def spy(store, envelope, **kwargs):
        seen.append(kwargs.get("mark_nonce"))
        return real_verify(store, envelope, **kwargs)

    monkeypatch.setattr(callerproof, "verify_proof", spy)
    quote = client.post(
        "/check/decision", json=_decision_body("mpp-parity-quote"))
    assert quote.status_code == 402, quote.text
    assert seen == [False]


@pytest.mark.parametrize("path", PROOF_ROUTES)
def test_unsigned_mpp_attempt_is_not_discovery_and_never_settles(
        client, settle_spy, path):
    response = client.post(
        path, json={}, headers={"Authorization": _bad_mpp()})
    assert response.status_code in (401, 402, 422), response.text
    if response.status_code == 402:
        body = response.json()
        detail = body.get("detail") if isinstance(body.get("detail"), dict) \
            else body
        assert not detail.get("discovery_only"), (
            f"{path} misclassified an MPP payment attempt as discovery")
    assert settle_spy == []


@pytest.mark.parametrize("path", PROOF_ROUTES)
def test_pure_unpaid_probe_still_gets_discovery_quote(client, path):
    response = client.post(path, json={})
    assert response.status_code == 402, (path, response.text)
    body = response.json()
    detail = body.get("detail") if isinstance(body.get("detail"), dict) \
        else body
    assert detail.get("discovery_only") is True


def test_dual_payment_protocols_fail_before_decode_or_settlement(
        client, settle_spy, monkeypatch):
    decoded = []
    monkeypatch.setattr(
        x402, "decode_payment_signature", lambda value: decoded.append(value))
    response = client.get(
        "/check", params={"capability": "fact-check"}, headers={
            "PAYMENT-SIGNATURE": "not-a-payment",
            "Authorization": _bad_mpp(),
        })
    assert response.status_code == 400, response.text
    assert decoded == []
    assert settle_spy == []


def test_mpp_kill_switch_reduces_helpers_to_x402_only(monkeypatch):
    monkeypatch.setenv("GUILD_MPP_ENABLED", "0")
    auth_token = main._mpp_auth.set(_bad_mpp())
    sig_token = main._xpay_sig.set("")
    v1_token = main._xpay_v1.set("")
    try:
        assert main._executable_payment_present() is False
        assert main._payment_attempt_present() is False

        main._xpay_sig.set("x402-v2")
        assert main._executable_payment_present() is True
        assert main._payment_attempt_present() is True

        main._xpay_sig.set("")
        main._xpay_v1.set("x402-v1")
        assert main._executable_payment_present() is False
        assert main._payment_attempt_present() is True
    finally:
        main._mpp_auth.reset(auth_token)
        main._xpay_sig.reset(sig_token)
        main._xpay_v1.reset(v1_token)


def test_mpp_payment_attempt_suppresses_generic_probe_helper(monkeypatch):
    auth_token = main._mpp_auth.set(_bad_mpp())
    sig_token = main._xpay_sig.set("")
    v1_token = main._xpay_v1.set("")
    try:
        assert main._payment_attempt_present() is True
        assert main._executable_payment_present() is True
        assert main._is_unpaid_probe(None) is False
    finally:
        main._mpp_auth.reset(auth_token)
        main._xpay_sig.reset(sig_token)
        main._xpay_v1.reset(v1_token)
