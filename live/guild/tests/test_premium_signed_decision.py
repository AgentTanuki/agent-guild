"""Premium signed-decision pricing and exact x402 binding."""
import base64
import json
import os

os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app import payments, x402  # noqa: E402
from app.billing import PRICING  # noqa: E402
from app.main import app  # noqa: E402


def _payment_required(response) -> dict:
    raw = response.headers[x402.PAYMENT_REQUIRED_HEADER]
    return json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))


def test_signed_decision_is_a_distinct_premium_operation():
    plain = payments.check_request("code-review")
    signed = payments.check_request("code-review", signed=True,
                                    ttl_seconds=7200)

    assert plain.operation == "best_agent"
    assert plain.cost == 10
    assert signed.operation == "signed_decision"
    assert signed.cost == 1000
    assert signed.cost == PRICING["signed_decision"]
    assert "signed=true" in signed.resource_url
    assert "ttl_seconds=7200" in signed.resource_url
    assert signed.request_hash != plain.request_hash


def test_unsigned_check_keeps_the_one_cent_entry_price(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    monkeypatch.setenv("GUILD_X402_NETWORK", "eip155:84532")
    with TestClient(app) as client:
        response = client.get("/check", params={"capability": "code-review"})

    assert response.status_code == 402
    challenge = _payment_required(response)
    assert challenge["accepts"][0]["amount"] == "10000"


def test_signed_check_quotes_one_usdc_and_exact_resource(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    monkeypatch.setenv("GUILD_X402_NETWORK", "eip155:84532")
    with TestClient(app) as client:
        response = client.get(
            "/check",
            params={"capability": "code-review", "signed": "true",
                    "ttl_seconds": 7200},
        )

    assert response.status_code == 402
    challenge = _payment_required(response)
    assert challenge["accepts"][0]["amount"] == "1000000"
    assert challenge["resource"]["url"].endswith(
        "/check?capability=code-review&signed=true&ttl_seconds=7200")
    assert "bazaar" in challenge["extensions"]
