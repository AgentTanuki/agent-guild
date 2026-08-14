"""OpenAPI-first machines can find and budget every payable HTTP utility."""
from fastapi.testclient import TestClient

from app import billing, mpp, openapi_payment_discovery, pricing, x402
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
        product = operation["x-agent-guild-product"]
        assert operation["security"] == []
        assert len(operation["summary"]) >= 70
        assert operation["description"].startswith(operation["summary"])
        assert len(product["use_cases"]) >= 2
        assert len(product["buyer_intents"]) >= 2
        assert product["output"]
        assert info["price"]["mode"] in {"fixed", "dynamic"}
        assert info["price"]["currency"] == "USD"
        assert info["protocols"][0] == {"x402": {"version": 2}}
        if mpp.enabled():
            assert info["protocols"][1] == {"mpp": {
                "method": "evm", "intent": "charge",
                "currency": x402.asset(),
            }}
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
    assert "payment-safety APIs for autonomous agents" in schema["info"][
        "description"]
    first_paths = list(schema["paths"])[:len({
        path for path, _ in openapi_payment_discovery.advertised_operations()
    })]
    assert set(first_paths) == {
        path for path, _ in openapi_payment_discovery.advertised_operations()
    }


def test_machine_product_language_names_the_purchase_decision_and_proof():
    paths = TestClient(app).get("/openapi.json").json()["paths"]

    signed = paths["/check/decision"]["post"]
    assert "signed AGD-1 trust decision" in signed["summary"]
    assert "offline-verifiable" in signed["summary"]

    payment = paths["/wallet-binding/decision"]["post"]
    for term in ("payee wallet", "chain", "token", "amount", "resource"):
        assert term in payment["summary"]

    envelope = paths["/envelopes/issue"]["post"]
    assert "machine-to-machine message" in envelope["summary"]
    assert "replay protection" in envelope["summary"]

    protected = paths["/wallet-binding/protected-decision"]["post"]
    assert "25 bps" in protected["summary"]

    assert "which agent should I hire for this capability" in \
        paths["/check"]["get"]["x-agent-guild-product"]["buyer_intents"]
    assert "sign a machine-to-machine message" in \
        envelope["x-agent-guild-product"]["buyer_intents"]
    assert "is this wallet safe to pay" in \
        payment["x-agent-guild-product"]["buyer_intents"]


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


def test_mpp_discovery_is_complete_when_acceptance_is_live(monkeypatch):
    monkeypatch.setenv("GUILD_MPP_ENABLED", "1")
    monkeypatch.setenv("GUILD_MPP_SECRET", "s" * 32)
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)

    schema = TestClient(app).get("/openapi.json").json()
    paid = _paid(schema)
    assert paid
    for operation in paid.values():
        assert operation["x-payment-info"]["protocols"] == [
            {"x402": {"version": 2}},
            {"mpp": {
                "method": "evm", "intent": "charge",
                "currency": x402.asset(),
            }},
        ]
        payment = operation["x-agent-guild-payment"]
        assert payment["mpp_authorization_header"] == \
            "Authorization: Payment"
        assert payment["mpp_receipt_header"] == "Payment-Receipt"


def test_mpp_kill_switch_removes_only_mpp_advertisement(monkeypatch):
    monkeypatch.setenv("GUILD_MPP_ENABLED", "0")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)

    schema = TestClient(app).get("/openapi.json").json()
    for operation in _paid(schema).values():
        assert operation["x-payment-info"]["protocols"] == [
            {"x402": {"version": 2}},
        ]
        payment = operation["x-agent-guild-payment"]
        assert payment["mpp_authorization_header"] is None
        assert payment["mpp_receipt_header"] is None
