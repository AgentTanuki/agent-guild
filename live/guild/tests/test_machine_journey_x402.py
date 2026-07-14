"""Black-box machine journey — a CLEAN client (no account, no copied API key,
no human) must be able to progress:

  discover → self-provision identity → obtain trial → check trust →
  receive 402 when appropriate → pay (x402 v2) → receive result →
  verify receipt

driven ONLY by what the service itself serves (manifest + 402 challenges).
The x402 facilitator is the deterministic fake from test_x402_v2 — no chain,
no funds, and the settlement stays labelled testnet/value-less throughout."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from app import x402
from tests.test_x402_v2 import FakeFacilitator, make_payload, sig_header, PAY_TO


@pytest.fixture()
def clean_market(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    from app.main import app
    with TestClient(app) as client:
        yield client


def test_clean_client_full_journey(clean_market):
    client = clean_market
    # snapshot revenue BEFORE the journey (the store singleton is shared
    # across the suite): this journey must not move REAL revenue at all
    rev_before = client.get("/billing/revenue").json()

    # 1. DISCOVER — the machine manifest says exactly what is free, what is
    # credit-funded and what is x402-funded, with complete retry instructions.
    manifest = client.get("/.well-known/agent-guild.json").json()
    pay = manifest["payments"]
    assert "register" in pay["operation_funding"]["free"]
    assert "best_agent" in pay["operation_funding"]["x402_funded"]
    assert pay["x402"]["version"] == 2
    assert pay["x402"]["network"].startswith("eip155:")
    assert "PAYMENT-SIGNATURE" in pay["x402"]["retry_instructions"]["3"]
    assert "TESTNET" in (pay["x402"]["network_value_warning"] or "")

    # 2. SELF-PROVISION IDENTITY — register with no human, no dashboard.
    reg = client.post("/agents/register", json={
        "name": "journey-bot", "capabilities": ["journey.test"]})
    assert reg.status_code == 200
    agent = reg.json()

    # 3. OBTAIN TRIAL — self-serve sandbox credits (labelled NOT money).
    trial_path = manifest["economics"]["acquire_credits"]["trial"]["path"]
    trial = client.post(trial_path)
    assert trial.status_code == 200
    key = trial.json()["key"]
    assert agent["id"]  # identity provisioned in step 2 (no human involved)

    # 4. CHECK TRUST on the trial balance.
    r = client.get("/check?capability=journey.test",
                   headers={"X-API-Key": key})
    assert r.status_code == 200

    # 5. RECEIVE 402 WHEN APPROPRIATE — drain the trial; the 402 carries the
    # standards-compliant v2 challenge.
    challenge = None
    for _ in range(200):
        r = client.get("/check?capability=journey.test",
                       headers={"X-API-Key": key})
        if r.status_code == 402:
            challenge = r
            break
    assert challenge is not None, "trial never exhausted into a 402"
    hdr = challenge.headers.get("PAYMENT-REQUIRED")
    assert hdr, "402 must carry the PAYMENT-REQUIRED header"
    required = json.loads(base64.b64decode(hdr))
    assert required["x402Version"] == 2
    accepts = required["accepts"][0]
    assert accepts["network"].startswith("eip155:")
    assert "bazaar" in (required.get("extensions") or {})

    # 6. PAY — construct the v2 payment from the challenge (exact scheme,
    # quoted amount/asset/recipient, echoing the quoted resource).
    cost_credits = int(accepts["amount"]) // x402.ATOMIC_PER_CREDIT
    payload = make_payload("best_agent", cost_credits,
                           url=required["resource"]["url"])
    paid = client.get("/check?capability=journey.test",
                      headers={"PAYMENT-SIGNATURE": sig_header(payload)})

    # 7. RECEIVE RESULT.
    assert paid.status_code == 200
    assert "verdict" in paid.json()

    # 8. VERIFY RECEIPT — the settlement header decodes to a v2
    # SettleResponse with a transaction hash, and the service's own revenue
    # accounting stays honest: testnet settlement, zero real revenue.
    receipt = json.loads(base64.b64decode(paid.headers["PAYMENT-RESPONSE"]))
    assert receipt["success"] is True
    assert receipt["transaction"].startswith("0x")
    assert receipt["network"] == accepts["network"]
    revenue = client.get("/billing/revenue").json()
    assert (revenue["real_settlement"]["revenue_usd"]
            == rev_before["real_settlement"]["revenue_usd"]), \
        "a testnet journey must never move real revenue"
    assert (revenue["testnet_settlement"]["transactions"]
            == rev_before["testnet_settlement"]["transactions"] + 1)
