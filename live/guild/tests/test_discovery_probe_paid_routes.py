"""Every advertised paid route must expose its unpaid discovery quote.

This reproduces MPPScan's production audit: eight parameterized operations
looked unpayable because resource/query/body validation returned 404/422
before the shared payment gateway could return 402.  The crawler-style matrix
below reads the service's own contract shape and exercises every affected
operation without any credential or settlement.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from urllib.parse import urlparse

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import payments, pricing  # noqa: E402


@pytest.fixture()
def enforced(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_NETWORK", "eip155:8453")
    monkeypatch.setenv(
        "GUILD_X402_ASSET",
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    pricing.load_runtime({})


@pytest.fixture()
def client(enforced):
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def settle_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(
        payments, "settle_x402", lambda *args, **kwargs: calls.append(1))
    return calls


FAILING_ROUTES = [
    ("GET", "/search", {}),
    ("GET", "/agents/{agent_id}/reputation", {"agent_id": "agent_probe_x"}),
    ("GET", "/agents/{agent_id}/journey", {"agent_id": "agent_probe_x"}),
    ("GET", "/agents/{agent_id}/evidence", {"agent_id": "agent_probe_x"}),
    ("GET", "/agents/{agent_id}/flags", {"agent_id": "agent_probe_x"}),
    ("GET", "/agents/{agent_id}/risk-score", {"agent_id": "agent_probe_x"}),
    ("GET", "/preflight/deep", {}),
    ("POST", "/evidence/bundle", {}),
    ("POST", "/wallet-binding/protected-decision/tiers/{tier_id}",
     {"tier_id": "1000-usdc"}),
]

CONTROL_ROUTES = [
    ("GET", "/check", {}, {"capability": "fact-check"}),
    ("POST", "/wallet-binding/decision", {}, None),
]


def _send(client, method, path, path_params, query=None, headers=None,
          body=None):
    url = path
    for key, value in path_params.items():
        url = url.replace("{" + key + "}", value)
    probe_headers = {"user-agent": "MPPScan-probe/1.0", **(headers or {})}
    if method == "GET":
        return client.get(url, params=query or {}, headers=probe_headers)
    return client.post(
        url, params=query or {}, json={} if body is None else body,
        headers=probe_headers)


@pytest.mark.parametrize(
    "method,path,path_params", FAILING_ROUTES,
    ids=[route[1] for route in FAILING_ROUTES])
def test_unpaid_probe_gets_402(
        client, settle_spy, method, path, path_params):
    response = _send(client, method, path, path_params)
    assert response.status_code == 402, (
        f"{method} {path} returned {response.status_code}: "
        f"{response.text[:160]}")
    assert settle_spy == []
    assert "payment-response" not in {key.lower() for key in response.headers}


@pytest.mark.parametrize(
    "method,path,path_params,query", CONTROL_ROUTES,
    ids=[route[1] for route in CONTROL_ROUTES])
def test_controls_still_402(
        client, settle_spy, method, path, path_params, query):
    assert _send(client, method, path, path_params, query).status_code == 402
    assert settle_spy == []


def test_body_bound_manifest_probe_returns_non_executable_body_template(
        client, settle_spy):
    from app.main import store

    def event_count(event_type):
        return len([event for event in store.events
                    if event.get("type") == event_type])

    document = client.get("/.well-known/x402").json()
    probe = document["body_bound_products"]["discovery_probe"]
    targets = probe["targets"]
    assert {(target["operation"], urlparse(target["url"]).path)
            for target in targets} == {
        ("signed_decision", "/check/decision"),
        ("evidence_bundle", "/evidence/bundle"),
        ("machine_envelope", "/envelopes/issue"),
        ("payment_decision", "/wallet-binding/decision"),
        ("protected_payment_decision",
         "/wallet-binding/protected-decision"),
        ("protected_payment_decision",
         "/wallet-binding/protected-decision/tiers/1000-usdc"),
    }
    assert probe["body_template"] == {
        "source": "base64-decoded PAYMENT-REQUIRED header",
        "json_pointer": "/extensions/bazaar/info/input/body",
    }
    proof_required_paths = {
        "/check/decision",
        "/envelopes/issue",
        "/wallet-binding/protected-decision",
        "/wallet-binding/protected-decision/tiers/1000-usdc",
    }

    for target in targets:
        before_paid = event_count("paid_offer_shown")
        before_passport = event_count("offer_served")
        path = urlparse(target["url"]).path
        response = _send(
            client, target["method"], path, {},
            headers=probe["request"]["headers"],
            body=probe["request"]["body"])

        assert response.status_code == 402, response.text[:300]
        assert response.headers["X-Agent-Guild-Discovery-Probe"] == \
            "non-attributed"
        detail = response.json()["detail"]
        assert detail["discovery_only"] is True
        assert detail["executable"] is False
        challenge = json.loads(base64.b64decode(
            response.headers["PAYMENT-REQUIRED"]))
        input_info = challenge["extensions"]["bazaar"]["info"]["input"]
        assert input_info["method"] == "POST"
        assert input_info["bodyType"] == "json"
        assert isinstance(input_info["body"], dict)
        assert input_info["body"], (
            "the discovery quote must contain a body template")
        assert "discovery-only" not in json.dumps(input_info["body"])
        assert ("caller_proof" in input_info["body"]) == \
            (path in proof_required_paths)
        assert "payment-response" not in {
            key.lower() for key in response.headers}
        assert settle_spy == []
        assert event_count("paid_offer_shown") == before_paid
        assert event_count("offer_served") == before_passport

        # Following the published handoff can never reproduce the deliberately
        # non-executable discovery quote.  Proof-bound examples first demand a
        # fresh valid proof; the two plain-body products return a new exact 402.
        resend = _send(
            client, target["method"], path, {}, body=input_info["body"],
            headers={"user-agent": "machine-buyer/1.0"})
        if path in proof_required_paths:
            assert resend.status_code == 401
        else:
            assert resend.status_code == 402
            resend_required = json.loads(base64.b64decode(
                resend.headers["PAYMENT-REQUIRED"]))
            assert resend_required["resource"]["url"] != \
                challenge["resource"]["url"]
        assert "payment-response" not in {
            key.lower() for key in resend.headers}
        assert settle_spy == []


def test_evidence_discovery_template_resend_gets_distinct_executable_quote(
        client, settle_spy):
    discovery = client.post(
        "/evidence/bundle", json={}, headers={
            "user-agent": "machine-catalogue/1.0",
            "X-Agent-Guild-Discovery-Probe": "manifest",
        })
    assert discovery.status_code == 402
    discovery_required = json.loads(base64.b64decode(
        discovery.headers["PAYMENT-REQUIRED"]))
    template = discovery_required["extensions"]["bazaar"]["info"][
        "input"]["body"]
    assert template["url"].startswith("https://")
    assert template["url"] != "discovery-only"

    request_quote = client.post(
        "/evidence/bundle", json=template,
        headers={"user-agent": "machine-buyer/1.0"})
    assert request_quote.status_code == 402
    request_required = json.loads(base64.b64decode(
        request_quote.headers["PAYMENT-REQUIRED"]))
    assert request_required["resource"]["url"] != \
        discovery_required["resource"]["url"]
    assert "discovery-only" not in request_required["resource"]["url"]
    assert "payment-response" not in {
        key.lower() for key in request_quote.headers}
    assert settle_spy == []


def test_openapi_declares_a_real_tier_example(client):
    schema = client.get("/openapi.json").json()
    operation = schema["paths"][
        "/wallet-binding/protected-decision/tiers/{tier_id}"]["post"]
    parameter = next(
        item for item in operation["parameters"] if item["name"] == "tier_id")
    parameter_schema = parameter.get("schema") or {}
    assert "1000-usdc" in (parameter_schema.get("examples") or [])
    assert (
        parameter.get("example") == "1000-usdc"
        or parameter_schema.get("example") == "1000-usdc"
    ), "registry compatibility requires the singular OpenAPI example field"


def test_unknown_tier_registry_placeholder_gets_canonical_safe_quote(
        client, settle_spy):
    response = client.post(
        "/wallet-binding/protected-decision/tiers/not_found",
        json={}, headers={"user-agent": "registry-probe/1.0"})
    assert response.status_code == 402
    payload = response.json()["detail"]
    assert payload["discovery_only"] is True
    assert payload["executable"] is False
    assert payload["requested_tier"] == "not_found"
    assert payload["quoted_tier"] == "1000-usdc"
    challenge = json.loads(base64.b64decode(
        response.headers["PAYMENT-REQUIRED"]))
    assert challenge["resource"]["url"].split("?", 1)[0].endswith(
        "/wallet-binding/protected-decision/tiers/1000-usdc")
    assert "not_found" not in challenge["resource"]["url"]
    assert settle_spy == []


def test_unknown_tier_payment_retry_fails_before_settlement(
        client, settle_spy):
    response = client.post(
        "/wallet-binding/protected-decision/tiers/not_found",
        json={}, headers={"PAYMENT-SIGNATURE": "AAAA"})
    assert response.status_code == 404
    assert settle_spy == []
    assert "payment-response" not in {
        key.lower() for key in response.headers}


def test_x402_retry_for_unknown_agent_fails_before_settlement(
        client, settle_spy):
    response = client.get(
        "/agents/definitely_not_a_real_agent/reputation",
        headers={"user-agent": "buyer", "PAYMENT-SIGNATURE": "AAAA"})
    assert response.status_code == 404
    assert settle_spy == []
    assert "payment-response" not in {
        key.lower() for key in response.headers}


def test_api_key_caller_is_not_treated_as_a_probe(client):
    response = client.get(
        "/agents/definitely_not_a_real_agent/reputation",
        headers={"user-agent": "buyer", "X-API-Key": "sk_probe"})
    assert response.status_code == 404


@pytest.mark.parametrize("headers", [
    {"PAYMENT-SIGNATURE": "AAAA"},
    {"Authorization": "Payment invalid"},
    {"X-API-Key": "sk_probe"},
])
def test_search_missing_capability_payment_or_key_fails_before_settlement(
        client, settle_spy, headers):
    response = client.get("/search", headers=headers)
    assert response.status_code == 422
    assert settle_spy == []
    assert "payment-response" not in {
        key.lower() for key in response.headers}


def test_search_bare_registry_probe_is_non_executable(client, settle_spy):
    response = client.get(
        "/search", headers={"user-agent": "AgenticMarket/1.0"})
    assert response.status_code == 402
    detail = response.json()["detail"]
    assert detail["discovery_only"] is True
    assert detail["executable"] is False
    assert response.headers.get("PAYMENT-REQUIRED")
    challenge = json.loads(base64.b64decode(
        response.headers["PAYMENT-REQUIRED"]))
    assert response.json()["accepts"] == challenge["accepts"]
    assert response.json()["resource"] == challenge["resource"]
    assert response.json()["extensions"] == challenge["extensions"]
    output_schema = challenge["extensions"]["bazaar"]["info"][
        "output"]["schema"]
    assert set(output_schema["required"]) == {
        "capability", "count", "results"}
    assert response.headers.get("WWW-Authenticate", "").startswith("Payment ")
    assert settle_spy == []


@pytest.mark.parametrize(
    "method,path", [("GET", "/preflight/deep"),
                    ("POST", "/evidence/bundle")])
def test_paid_missing_input_fails_closed_before_settlement(
        client, settle_spy, method, path):
    response = _send(
        client, method, path, {},
        headers={"PAYMENT-SIGNATURE": "AAAA"})
    assert response.status_code == 422
    assert settle_spy == []
    assert "payment-response" not in {
        key.lower() for key in response.headers}
