"""One-variable x402 conversion experiment: quote copy, not capability."""
import base64
import json

from fastapi.testclient import TestClient

from app import billing, payments, x402
from app.main import app
from app.state import store


def _challenge(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    request = payments.check_request("fact-check")
    model = x402.payment_required_model(request, 10)
    return request, model


def test_trial_cta_is_off_by_default_but_sandbox_label_and_passport_remain(
        monkeypatch):
    monkeypatch.delenv("GUILD_X402_TRIAL_CTA", raising=False)
    request, model = _challenge(monkeypatch)
    body = x402.payment_required_body(request, 10, model=model)

    assert "/billing/trial" not in json.dumps(body)
    assert body["sandbox"]["unit"] == "credits_sandbox"
    assert "not money" in body["sandbox"]["note"]
    assert body["claim_passport"]["register"].startswith("POST /agents/register")


def test_trial_cta_can_be_restored_with_one_flag_without_changing_quote(
        monkeypatch):
    request, model = _challenge(monkeypatch)
    canonical_header = x402.payment_required_header_value(model)

    monkeypatch.setenv("GUILD_X402_TRIAL_CTA", "1")
    treatment = x402.payment_required_body(request, 10, model=model)
    assert "/billing/trial" in treatment["sandbox"]["note"]
    assert x402.payment_required_header_value(model) == canonical_header
    decoded = json.loads(base64.b64decode(canonical_header))
    assert decoded == model.model_dump(by_alias=True, exclude_none=True)


def test_trial_faucet_still_functions_when_not_advertised(monkeypatch):
    monkeypatch.delenv("GUILD_X402_TRIAL_CTA", raising=False)
    with TestClient(app) as client:
        account = client.post("/billing/trial").json()
    assert account["balance"] >= billing.TRIAL_CREDITS
    assert isinstance(account["key"], str) and account["key"]


def test_live_402_legacy_detail_obeys_flag_but_canonical_header_does_not(
        monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    monkeypatch.delenv("GUILD_X402_TRIAL_CTA", raising=False)

    with TestClient(app) as client:
        response = client.get("/search?capability=fact-check")

    assert response.status_code == 402
    assert "/billing/trial" not in json.dumps(response.json())
    quoted = json.loads(base64.b64decode(response.headers["PAYMENT-REQUIRED"]))
    for field in ("x402Version", "error", "resource", "accepts", "extensions"):
        assert response.json()[field] == quoted[field]
    assert "sandbox" not in quoted


def test_live_402_can_restore_structured_trial_cta_with_flag(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    monkeypatch.setenv("GUILD_X402_TRIAL_CTA", "1")

    with TestClient(app) as client:
        response = client.get("/search?capability=fact-check")

    detail = response.json()["detail"]
    assert response.status_code == 402
    assert detail["acquire"]["trial"]["path"] == "/billing/trial"
    assert "/billing/trial" in detail["sandbox"]["note"]


def test_invalid_sandbox_key_does_not_leak_trial_cta_when_flag_is_off(
        monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    monkeypatch.delenv("GUILD_X402_TRIAL_CTA", raising=False)

    with TestClient(app) as client:
        response = client.get(
            "/search?capability=fact-check",
            headers={"X-API-Key": "ak_invalid"},
        )

    assert response.status_code == 402
    assert response.json()["detail"]["error"] == "unknown_billing_key"
    assert "/billing/trial" not in json.dumps(response.json())


def test_insufficient_sandbox_balance_does_not_leak_trial_cta(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    monkeypatch.delenv("GUILD_X402_TRIAL_CTA", raising=False)

    with TestClient(app) as client:
        account = client.post("/billing/trial").json()
        store.charge(account["key"], account["balance"], "best_agent")
        response = client.get(
            "/search?capability=fact-check",
            headers={"X-API-Key": account["key"]},
        )

    assert response.status_code == 402
    detail = response.json()["detail"]
    assert detail["error"] == "insufficient_credits"
    assert "trial" not in detail["acquire"]
    assert "/billing/trial" not in json.dumps(response.json())


def test_every_anonymous_priced_http_quote_is_trial_cta_free(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    monkeypatch.delenv("GUILD_X402_TRIAL_CTA", raising=False)
    cases = (
        ("GET", "/search?capability=fact-check", None),
        ("GET", "/check?capability=fact-check", None),
        ("POST", "/check/decision", {}),
        ("POST", "/wallet-binding/decision", {}),
        ("POST", "/wallet-binding/protected-decision/tiers/standard", {}),
        ("POST", "/envelopes/issue", {}),
        ("GET", "/preflight/deep", None),
        ("POST", "/evidence/bundle", {}),
    )

    with TestClient(app) as client:
        for method, path, body in cases:
            response = client.request(
                method,
                path,
                json=body,
                headers={"X-Agent-Guild-Discovery-Probe": "manifest"},
            )
            assert response.status_code == 402, (path, response.text)
            assert "/billing/trial" not in response.text, path
            assert response.headers.get("PAYMENT-REQUIRED"), path
