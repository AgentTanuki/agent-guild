"""OpenAPI-first machines can find and budget every payable HTTP utility."""
from fastapi.testclient import TestClient

from app import billing, openapi_payment_discovery, pricing
from app.main import app


def _paid(schema):
    out = {}
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if isinstance(operation, dict) and "x-payment-info" in operation:
                out[(path, method)] = operation
    return out


def test_every_declared_payment_operation_is_live_and_structured():
    schema = TestClient(app).get("/openapi.json").json()
    paid = _paid(schema)

    assert set(paid) == openapi_payment_discovery.advertised_operations()
    for operation in paid.values():
        info = operation["x-payment-info"]
        assert operation["security"] == []
        assert info["price"]["mode"] in {"fixed", "dynamic"}
        assert info["price"]["currency"] == "USD"
        assert info["protocols"] == [{"x402": {"version": 2}}]
        assert "402" in operation["responses"]
        assert operation["x-agent-guild-payment"][
            "live_quote_authoritative"] is True

    assert schema["x-agentcash-guidance"]["llmsTxtUrl"].endswith("/llms.txt")
    assert schema["x-agentcash-provenance"]["ownershipProofs"]
    assert schema["info"]["contact"]["url"].endswith(
        "/AgentTanuki/agent-guild/issues")
    assert schema["info"]["termsOfService"].endswith("/terms.json")
    assert "Payable machine utilities are ordered first" in schema["info"][
        "x-guidance"]
    first_paths = list(schema["paths"])[:len({
        path for path, _ in openapi_payment_discovery.advertised_operations()
    })]
    assert set(first_paths) == {
        path for path, _ in openapi_payment_discovery.advertised_operations()
    }


def test_runtime_prices_are_not_stale_in_fastapi_openapi_cache():
    client = TestClient(app)
    original = pricing.runtime_overrides()
    try:
        pricing.load_runtime({"machine_envelope": 23})
        first = client.get("/openapi.json").json()
        assert first["paths"]["/envelopes/issue"]["post"][
            "x-payment-info"]["price"]["amount"] == "0.023"

        pricing.load_runtime({"machine_envelope": 47})
        second = client.get("/openapi.json").json()
        assert second["paths"]["/envelopes/issue"]["post"][
            "x-payment-info"]["price"]["amount"] == "0.047"
    finally:
        pricing.load_runtime(original)


def test_advisory_prices_match_the_gateway_price_sources():
    schema = TestClient(app).get("/openapi.json").json()
    paths = schema["paths"]

    envelope = paths["/envelopes/issue"]["post"]["x-payment-info"]["price"]
    assert envelope == {
        "mode": "fixed", "currency": "USD",
        "amount": str(pricing.price("machine_envelope") / 1000),
    }

    signed = paths["/check/decision"]["post"]["x-payment-info"]["price"]
    assert signed["amount"] == "1"

    conditional = paths["/check"]["get"]["x-payment-info"]["price"]
    assert conditional == {
        "mode": "dynamic", "currency": "USD",
        "min": "0.01", "max": "1",
    }


def test_free_verification_and_catalog_routes_are_not_marked_paid():
    schema = TestClient(app).get("/openapi.json").json()
    for path, method in (
        ("/envelopes/verify", "post"),
        ("/wallet-binding/decision/verify", "post"),
        ("/wallet-binding/protected-decision/tiers", "get"),
        ("/credentials/verify", "post"),
        ("/pricing", "get"),
    ):
        assert "x-payment-info" not in schema["paths"][path][method]
