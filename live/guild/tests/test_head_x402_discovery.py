"""HEAD registry probes quote paid GET trust checks without execution."""

import base64
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import payments, x402
from app.state import store


PAY_TO = "0x" + "11" * 20


@pytest.fixture(autouse=True)
def _enforced(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    yield


def _required(response):
    return json.loads(base64.b64decode(
        response.headers[x402.PAYMENT_REQUIRED_HEADER]))


def test_head_check_quotes_exact_get_without_recording_demand():
    from app.main import app

    cap = "registry-head-" + uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        # Startup may seed labelled baseline evidence; snapshot after it.
        events_before = len(store.events)
        response = client.head(f"/check?capability={cap}")

        assert response.status_code == 402
        assert response.content == b""
        required = _required(response)
        assert required["x402Version"] == 2
        assert required["resource"]["url"].endswith(
            f"/check?capability={cap}&signed=false&ttl_seconds=3600"
        )
        assert required["extensions"]["bazaar"]["info"]["input"] == {
            "type": "http",
            "method": "GET",
            "queryParams": {
                "capability": cap,
                "signed": "false",
                "ttl_seconds": "3600",
            },
        }
        assert len(store.events) == events_before
        assert cap not in client.get("/capabilities").json()["unmet_demand"]


def test_payment_bearing_head_never_enters_settlement(monkeypatch):
    from app.main import app

    def forbidden(*args, **kwargs):
        raise AssertionError("HEAD must not enter authorization or settlement")

    monkeypatch.setattr(payments, "authorize", forbidden)
    with TestClient(app) as client:
        response = client.head(
            "/check?capability=fact-check",
            headers={
                x402.PAYMENT_SIGNATURE_HEADER: base64.b64encode(
                    b'{"x402Version":2}'
                ).decode(),
                "X-API-Key": "ak_must_not_be_charged",
            },
        )

    assert response.status_code == 402
    required = _required(response)
    assert required["resource"]["url"].endswith(
        "/check?capability=fact-check&signed=false&ttl_seconds=3600"
    )


def test_head_signed_quote_preserves_signed_get_semantics():
    from app.main import app

    with TestClient(app) as client:
        response = client.head(
            "/check?capability=research&signed=true&ttl_seconds=7200"
        )

    assert response.status_code == 402
    required = _required(response)
    assert required["resource"]["url"].endswith(
        "/check?capability=research&signed=true&ttl_seconds=7200"
    )
    assert required["extensions"]["bazaar"]["info"]["input"]["method"] == "GET"
