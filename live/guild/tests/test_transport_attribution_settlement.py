"""Settlement payer attribution across transports (corrective pass).

Rules under test:
  * a known first-party canary is marked first-party at settle time on
    EVERY transport it can pay over;
  * an UNCLASSIFIED payer stays unknown (first_party_payer = None) — it is
    NEVER affirmatively marked external. (The previous pass recorded
    first_party_payer=False for every plain HTTP payment, which the funnel
    then counted as external mainnet revenue — the exact inflation this
    project exists to avoid.)

A2A is audited but NOT patched here: live/guild/app/a2a.py and
a2a_x402.py carry concurrent uncommitted work and must not be touched.
The required A2A patch is documented in the corrective-pass report.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app import payments, x402, x402_confirm
from app.state import store
from tests.test_x402_v2 import FakeFacilitator, make_payload, sig_header
from tests.test_x402_cdp_settlement import FAKE_KEY_ID, FAKE_SECRET, _receipt

MAINNET = "eip155:8453"


@pytest.fixture(autouse=True)
def _mainnet(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_NETWORK", MAINNET)
    monkeypatch.setenv("GUILD_X402_PAY_TO", x402.MAINNET_TREASURY)
    monkeypatch.setenv("CDP_API_KEY_ID", FAKE_KEY_ID)
    monkeypatch.setenv("CDP_API_KEY_SECRET", FAKE_SECRET)
    monkeypatch.delenv("GUILD_X402_ASSET", raising=False)
    monkeypatch.delenv("GUILD_X402_FACILITATOR", raising=False)
    monkeypatch.setenv("GUILD_X402_CONFIRM_TIMEOUT", "0")
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_FIRST_PARTY_TOKEN", "fp-secret")
    monkeypatch.setattr(x402, "_facilitator",
                        lambda: FakeFacilitator(network=MAINNET))
    monkeypatch.setattr(x402_confirm, "_get_receipt",
                        lambda tx, timeout=15.0: _receipt())
    monkeypatch.setattr(x402_confirm, "current_block",
                        lambda network=None, timeout=15.0: 5_000_000)
    billing_before = len(store.billing_log)
    yield
    del store.billing_log[billing_before:]


def _cap():
    return "attr-" + uuid.uuid4().hex[:8]


def _last_payment():
    return [b for b in store.billing_log
            if b.get("type") == "x402_payment"][-1]


def test_http_unclassified_payer_is_unknown_never_external():
    from app.main import app
    cap = _cap()
    preq = payments.check_request(cap)
    with TestClient(app) as client:
        r = client.get(f"/check?capability={cap}",
                       headers={"User-Agent": "some-external-buyer/1",
                                "PAYMENT-SIGNATURE":
                                sig_header(make_payload(preq))})
        assert r.status_code == 200
        rec = _last_payment()
        assert rec.get("first_party_payer") is None, (
            "an unclassified HTTP payer must stay UNKNOWN (None) — "
            f"recording {rec.get('first_party_payer')!r} lets the funnel "
            "claim it as external revenue")
        funnel = {s["stage"]: s["count"]
                  for s in client.get("/funnel").json()["stages"]}
        assert funnel["external_mainnet_settlement"] == 0


def test_http_first_party_canary_is_marked_first_party():
    from app.main import app
    cap = _cap()
    preq = payments.check_request(cap)
    with TestClient(app) as client:
        r = client.get(f"/check?capability={cap}",
                       headers={"User-Agent": "guild-canary/1",
                                "X-Agent-Guild-First-Party": "fp-secret",
                                "X-Agent-Guild-Role": "test",
                                "PAYMENT-SIGNATURE":
                                sig_header(make_payload(preq))})
        assert r.status_code == 200
    assert _last_payment().get("first_party_payer") is True


def test_mcp_first_party_canary_is_marked_first_party(monkeypatch):
    from app import mcp_server
    cap = _cap()
    preq = payments.check_request(cap)
    monkeypatch.setattr(mcp_server, "_mcp_payment",
                        lambda ctx: make_payload(preq))
    monkeypatch.setattr(
        mcp_server, "_http_headers_for_attribution",
        lambda: {"x-agent-guild-first-party": "fp-secret",
                 "x-agent-guild-role": "test"})
    mcp_server._serve_paid(preq, lambda: {"ok": True}, None)
    assert _last_payment().get("first_party_payer") is True


def test_mcp_unclassified_payer_stays_unknown(monkeypatch):
    from app import mcp_server
    cap = _cap()
    preq = payments.check_request(cap)
    monkeypatch.setattr(mcp_server, "_mcp_payment",
                        lambda ctx: make_payload(preq))
    monkeypatch.setattr(mcp_server, "_http_headers_for_attribution",
                        lambda: {})
    mcp_server._serve_paid(preq, lambda: {"ok": True}, None)
    assert _last_payment().get("first_party_payer") is None
