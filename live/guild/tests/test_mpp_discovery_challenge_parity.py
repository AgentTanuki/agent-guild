"""Discovery 402s advertise every payment protocol the service accepts.

The OpenAPI catalogue declares MPP on paid operations.  Proof-before-payment
POST routes and the HEAD registry quote must therefore expose an MPP Payment
challenge as well as the x402 PAYMENT-REQUIRED challenge, while remaining
non-executable and analytics-neutral.
"""
from __future__ import annotations

import json
import os
import re
import sys
from base64 import urlsafe_b64decode

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import mpp, pricing, x402  # noqa: E402
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


DISCOVERY_402S = [
    ("POST", "/check/decision", {"json": {}}),
    ("POST", "/wallet-binding/decision", {"json": {}}),
    ("POST", "/wallet-binding/protected-decision", {"json": {}}),
    ("POST", "/wallet-binding/protected-decision/tiers/standard",
     {"json": {}}),
    ("POST", "/envelopes/issue", {"json": {"payload_digest": "probe"}}),
]

REFERENCE_GET = ("GET", "/search", {"params": {"capability": "translation"}})


def _b64url_json(value: str) -> dict:
    padding = "=" * (-len(value) % 4)
    return json.loads(urlsafe_b64decode(value + padding))


def _parse_payment_challenge(header: str) -> dict:
    assert header.startswith("Payment "), header[:40]
    return dict(re.findall(r'(\w+)="([^"]*)"', header))


class TestDualAdvertiseWhenEnabled:
    @pytest.mark.parametrize("method,path,kwargs", DISCOVERY_402S)
    def test_discovery_402_carries_mpp_challenge(self, client, method, path,
                                                 kwargs):
        response = client.request(method, path, **kwargs)
        assert response.status_code == 402, response.text[:200]
        assert x402.PAYMENT_REQUIRED_HEADER.lower() in {
            key.lower() for key in response.headers}
        www = response.headers.get("WWW-Authenticate")
        assert www, f"{method} {path}: discovery 402 lost the MPP challenge"
        challenge = _parse_payment_challenge(www)
        for field in ("id", "realm", "method", "intent", "request",
                      "expires", "opaque"):
            assert challenge.get(field), f"missing {field} in MPP challenge"
        assert challenge["method"] == "evm"
        assert challenge["intent"] == "charge"

    @pytest.mark.parametrize("method,path,kwargs", DISCOVERY_402S)
    def test_mpp_quote_matches_x402_quote(self, client, method, path, kwargs):
        response = client.request(method, path, **kwargs)
        assert response.status_code == 402
        www = response.headers.get("WWW-Authenticate")
        assert www
        quote = _b64url_json(_parse_payment_challenge(www)["request"])
        body = response.json()
        detail = body.get("detail", body)
        accepts = detail.get("accepts") or []
        assert accepts, "x402 402 body lost its accepts[]"
        assert quote["amount"] == accepts[0]["amount"]
        assert quote["currency"].lower() == accepts[0]["asset"].lower()
        assert quote["recipient"].lower() == accepts[0]["payTo"].lower()

    def test_get_reference_still_dual_advertises(self, client):
        method, path, kwargs = REFERENCE_GET
        response = client.request(method, path, **kwargs)
        assert response.status_code == 402
        assert response.headers.get(
            "WWW-Authenticate", "").startswith("Payment ")

    @pytest.mark.parametrize("method,path,kwargs", DISCOVERY_402S)
    def test_challenge_binds_route_in_opaque(self, client, method, path,
                                             kwargs):
        response = client.request(method, path, **kwargs)
        www = response.headers.get("WWW-Authenticate")
        assert www
        opaque = _b64url_json(_parse_payment_challenge(www)["opaque"])
        assert opaque.get("route", "").startswith(f"{method} "), opaque


class TestKillSwitchAndFailOpen:
    @pytest.mark.parametrize("method,path,kwargs", DISCOVERY_402S)
    def test_mpp_disabled_means_x402_only(self, client, method, path, kwargs,
                                          monkeypatch):
        monkeypatch.setenv("GUILD_MPP_ENABLED", "0")
        response = client.request(method, path, **kwargs)
        assert response.status_code == 402
        assert x402.PAYMENT_REQUIRED_HEADER.lower() in {
            key.lower() for key in response.headers}
        assert "WWW-Authenticate" not in response.headers

    @pytest.mark.parametrize("method,path,kwargs", DISCOVERY_402S)
    def test_mint_failure_never_masks_402(self, client, method, path, kwargs,
                                          monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("mint failed")

        monkeypatch.setattr(mpp, "mint_challenge", boom)
        response = client.request(method, path, **kwargs)
        assert response.status_code == 402
        assert x402.PAYMENT_REQUIRED_HEADER.lower() in {
            key.lower() for key in response.headers}


def test_head_registry_quote_dual_advertises_without_recording_demand(client):
    capability = "mpp-head-discovery-parity"
    events_before = len(main.store.events)
    response = client.head(f"/check?capability={capability}")
    assert response.status_code == 402
    assert x402.PAYMENT_REQUIRED_HEADER in response.headers
    assert response.headers[mpp.WWW_AUTHENTICATE].startswith("Payment ")
    assert response.headers["Cache-Control"] == "no-store"
    assert len(main.store.events) == events_before
    assert capability not in client.get("/capabilities").json()["unmet_demand"]


@pytest.mark.parametrize("path", [item[1] for item in DISCOVERY_402S])
def test_malformed_mpp_retry_never_settles_or_reenters_discovery(
        client, path, monkeypatch):
    settled = []
    monkeypatch.setattr(main.payments, "settle_x402",
                        lambda *args, **kwargs: settled.append(True))
    response = client.post(
        path, json={}, headers={"Authorization": "Payment malformed"})
    # A disabled/unknown tier may fail earlier at its 404 guard; every allowed
    # status is still pre-settlement and must not be rediscovered as a probe.
    assert response.status_code in (401, 402, 404, 422)
    if response.status_code == 402:
        detail = response.json().get("detail") or {}
        assert detail.get("discovery_only") is not True
    assert settled == []
