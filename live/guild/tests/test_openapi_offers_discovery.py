"""Spec-shaped ``x-payment-info.offers[]`` + top-level ``x-service-info``.

draft-payment-discovery-00 recognises ONLY the ``offers`` array (or the
single-offer shorthand) inside ``x-payment-info`` — the ``protocols`` member
is an unknown extension to a generic spec-compliant client, which therefore
parsed ZERO payable offers from this document before this projection.

Conformance pinned here:

* every advertised paid operation carries exactly one spec-shaped offer:
  intent=charge, method=evm, currency == the live asset, amount a string of
  ASCII digits (fixed) or null (dynamic) — §4.4.1;
* the fixed amount equals BOTH the x402 ``accepts[0].amount`` and the MPP
  challenge quote of the live 402 for the same route — one pricing source,
  three surfaces, zero drift;
* ``x-service-info`` §4.3 shape with resolvable docs URIs;
* ``protocols[]`` is untouched (additive change only);
* MPP disabled → no ``offers`` member at all (the prior document shape,
  unchanged) and no ``x-service-info`` regression.

No payment header is ever sent; nothing settles.
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

from app import mpp, openapi_payment_discovery, pricing, x402  # noqa: E402
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


def _schema(client) -> dict:
    r = client.get("/openapi.json")
    assert r.status_code == 200
    return r.json()


def _paid_ops(schema) -> dict[tuple[str, str], dict]:
    out = {}
    for (path, method) in openapi_payment_discovery.advertised_operations():
        op = (schema["paths"].get(path) or {}).get(method)
        assert isinstance(op, dict), f"advertised op missing: {method} {path}"
        out[(path, method)] = op
    return out


DIGITS = re.compile(r"^\d+$")


class TestOffersShape:
    def test_every_paid_op_has_exactly_one_spec_offer(self, client):
        for (path, method), op in _paid_ops(_schema(client)).items():
            xpi = op["x-payment-info"]
            offers = xpi.get("offers")
            assert isinstance(offers, list) and len(offers) == 1, (
                f"{method} {path}: expected exactly one offer, got {offers!r}")
            offer = offers[0]
            assert offer["intent"] == "charge"
            assert offer["method"] == "evm"
            assert offer["currency"].lower() == x402.asset().lower()
            assert offer.get("description")

    def test_amount_is_digits_or_null_matching_price_mode(self, client):
        for (path, method), op in _paid_ops(_schema(client)).items():
            xpi = op["x-payment-info"]
            amount = xpi["offers"][0]["amount"]
            if xpi["price"]["mode"] == "fixed":
                assert isinstance(amount, str) and DIGITS.match(amount), (
                    f"{method} {path}: fixed amount must be ASCII digits, "
                    f"got {amount!r}")
            else:
                assert amount is None, (
                    f"{method} {path}: dynamic amount must be null")

    def test_protocols_member_untouched(self, client):
        for (path, method), op in _paid_ops(_schema(client)).items():
            protos = op["x-payment-info"]["protocols"]
            names = [next(iter(p)) for p in protos]
            assert names == ["x402", "mpp"], (
                f"{method} {path}: protocols[] changed: {names}")


class TestOfferAmountMatchesLive402:
    @pytest.mark.parametrize("method,path,probe", [
        ("get", "/search", lambda c: c.get(
            "/search", params={"capability": "translation"})),
        ("get", "/preflight/deep", lambda c: c.get(
            "/preflight/deep", params={"url": "https://example.com"})),
        ("post", "/envelopes/issue", lambda c: c.post(
            "/envelopes/issue", json={"payload_digest": "probe"})),
        ("post", "/wallet-binding/decision", lambda c: c.post(
            "/wallet-binding/decision", json={})),
    ])
    def test_fixed_offer_equals_challenge_amount(self, client, method, path,
                                                 probe):
        """One pricing source, three surfaces: OpenAPI offer amount ==
        x402 accepts[0].amount == MPP challenge quote amount."""
        schema = _schema(client)
        offer_amount = schema["paths"][path][method][
            "x-payment-info"]["offers"][0]["amount"]
        r = probe(client)
        assert r.status_code == 402, r.text[:200]
        detail = r.json().get("detail") or r.json()
        accepts = detail.get("accepts") or []
        assert accepts and offer_amount == accepts[0]["amount"], (
            f"{path}: offer {offer_amount!r} != accepts "
            f"{accepts and accepts[0]['amount']!r}")
        www = r.headers.get("WWW-Authenticate", "")
        m = re.search(r'request="([^"]*)"', www)
        assert m, "MPP challenge missing from live 402"
        quote = json.loads(urlsafe_b64decode(
            m.group(1) + "=" * (-len(m.group(1)) % 4)))
        assert offer_amount == quote["amount"]


class TestServiceInfo:
    def test_service_info_shape(self, client):
        si = _schema(client).get("x-service-info")
        assert si, "x-service-info missing"
        assert si["categories"] == ["ai", "data"]
        docs = si["docs"]
        host = x402.public_host()
        assert docs["homepage"] == host
        assert docs["apiReference"] == host + "/openapi.json"
        assert docs["llms"] == host + "/llms.txt"

    def test_docs_routes_exist(self, client):
        """The advertised doc URIs resolve on this same app (no dead links
        in discovery metadata)."""
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/llms.txt").status_code == 200


class TestMppDisabledParity:
    def test_no_offers_when_mpp_disabled(self, client, monkeypatch):
        monkeypatch.setenv("GUILD_MPP_ENABLED", "0")
        for (path, method), op in _paid_ops(_schema(client)).items():
            xpi = op["x-payment-info"]
            assert "offers" not in xpi, (
                f"{method} {path}: offers[] must not be advertised while "
                "MPP acceptance is off")
            names = [next(iter(p)) for p in xpi["protocols"]]
            assert names == ["x402"], names
