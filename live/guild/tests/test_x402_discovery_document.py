"""Canonical origin-level discovery for machine buyers."""
import base64
import json
from urllib.parse import urlparse

from fastapi.testclient import TestClient

from app import pricing, x402
from app.main import app


PAY_TO = "0x" + "11" * 20


def _required(response):
    return json.loads(base64.b64decode(
        response.headers[x402.PAYMENT_REQUIRED_HEADER]))


def test_well_known_x402_fans_out_without_cross_product_price():
    body = TestClient(app).get("/.well-known/x402").json()

    assert body["version"] == 1
    assert body["x402Version"] == 2
    assert len(body["resources"]) == len(set(body["resources"]))
    assert len(body["resources"]) >= 10
    assert all(urlparse(url).scheme == "https" for url in body["resources"])
    assert all(url.startswith(x402.public_host() + "/")
               for url in body["resources"])
    assert any("/envelopes/issue?request_sha256=discovery-only" in url
               for url in body["resources"])
    assert any("/wallet-binding/decision?request_sha256=discovery-only" in url
               for url in body["resources"])
    assert any("/check?capability=fact-check" in url
               for url in body["resources"])

    # The products have different prices (including dynamic value pricing).
    # A shared accepts/payment object would cause simple crawlers to apply the
    # first product's price to every other route.
    assert "accepts" not in body
    assert "payment" not in body
    assert body["payment_requirements_source"] == \
        "per-resource HTTP 402 challenge"
    assert body["pricing"] == x402.public_host() + "/pricing"
    assert pricing.price("machine_envelope") != 1000


def test_discovery_document_is_free_and_does_not_offer_or_settle():
    client = TestClient(app)
    before = client.get("/billing/revenue").json()
    response = client.get("/.well-known/x402")
    after = client.get("/billing/revenue").json()

    assert response.status_code == 200
    assert before["real_settlement"] == after["real_settlement"]
    assert before["settled_count"] == after["settled_count"]


def test_every_published_product_is_probeable_and_has_its_own_quote(
        monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    client = TestClient(app, raise_server_exceptions=False)
    resources = client.get("/.well-known/x402").json()["resources"]

    observed = {}
    for url in resources:
        parsed = urlparse(url)
        if parsed.path in ("/check", "/preflight/deep"):
            response = client.get(parsed.path + "?" + parsed.query)
        else:
            # Marketplace routes intentionally make an empty anonymous POST
            # a non-executable discovery quote. A payment-bearing retry still
            # fails before settlement; their existing route suites enforce it.
            response = client.post(parsed.path, json={})
        assert response.status_code == 402, (url, response.text)
        required = _required(response)
        assert required["resource"]["url"] == url
        assert required["accepts"][0]["payTo"] == PAY_TO
        assert required["extensions"]["bazaar"]["info"]["input"]
        observed[parsed.path] = required["accepts"][0]["amount"]

    assert observed["/envelopes/issue"] == "10000"
    assert observed["/wallet-binding/decision"] == "10000"
    assert observed["/check"] == "1000000"
