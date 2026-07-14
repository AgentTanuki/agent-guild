"""x402 v2 rail: protocol surface + security regressions.

Covers, with a deterministic fake facilitator (no chain, no funds):
  * v2 challenge: HTTP 402 with the base64 PAYMENT-REQUIRED header and a
    spec-shaped body (x402Version 2, CAIP-2 network, resource object);
  * exact binding — replay, cross-resource substitution, price substitution,
    recipient substitution, expired / not-yet-valid authorisations and double
    settlement are all REJECTED and the protected result is never served;
  * v1 legacy (X-PAYMENT) stays accepted but deprecated and passes through
    the SAME guards — it cannot weaken v2 validation;
  * settlement records carry facilitator/network/asset/amount/payer/
    recipient/tx/status, and REAL revenue counts only mainnet settlements —
    testnet and mocked settlements never increase it.

The independent-official-client interoperability test (real HTTP, official
x402 SDK client, EVM signer) lives in tests_x402_interop/ and runs in its own
clean CI environment.
"""
import base64
import json
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from x402.schemas import PaymentPayload, ResourceInfo

from app import x402

PAY_TO = "0x" + "11" * 20
PAYER = "0x" + "22" * 20


@pytest.fixture(autouse=True)
def _x402_env(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    yield


class FakeFacilitator:
    """Deterministic v2 facilitator: structurally verifies the payload and
    'settles' with a synthetic tx hash. Never touches a chain."""

    def __init__(self, verify_ok=True, settle_ok=True, network=None):
        self.verify_ok, self.settle_ok = verify_ok, settle_ok
        self.network = network
        self.verify_calls, self.settle_calls = [], []

    def verify(self, payload, requirements):
        self.verify_calls.append((payload, requirements))
        return SimpleNamespace(is_valid=self.verify_ok,
                               invalid_reason=None if self.verify_ok
                               else "insufficient_funds",
                               payer=PAYER)

    def settle(self, payload, requirements):
        self.settle_calls.append((payload, requirements))
        net = self.network or requirements.network
        if not self.settle_ok:
            return SimpleNamespace(success=False, error_reason="invalid_transaction_state",
                                   transaction="", network=net, payer=PAYER)
        return SimpleNamespace(success=True, error_reason=None,
                               transaction="0x" + uuid.uuid4().hex * 2,
                               network=net, payer=PAYER)

    def close(self):
        pass


def make_payload(endpoint: str, cost: int, *, amount=None, pay_to=None,
                 url=None, nonce=None, valid_after=None, valid_before=None,
                 accepted=None, version=2) -> PaymentPayload:
    """A structurally valid v2 exact-EVM PaymentPayload for `endpoint`,
    with keyword hooks to tamper with any bound field."""
    offered = x402.requirements(endpoint, cost)
    now = time.time()
    auth = {
        "from": PAYER,
        "to": pay_to if pay_to is not None else offered.pay_to,
        "value": amount if amount is not None else offered.amount,
        "validAfter": str(int(valid_after if valid_after is not None else now - 60)),
        "validBefore": str(int(valid_before if valid_before is not None else now + 300)),
        "nonce": nonce or ("0x" + uuid.uuid4().hex + uuid.uuid4().hex),
    }
    return PaymentPayload(
        x402_version=version,
        accepted=accepted if accepted is not None else offered,
        resource=ResourceInfo(url=url or x402.resource_url(endpoint),
                              mime_type="application/json"),
        payload={"signature": "0x" + "ab" * 65, "authorization": auth},
    )


def sig_header(payload: PaymentPayload) -> str:
    return base64.b64encode(json.dumps(
        payload.model_dump(by_alias=True, exclude_none=True)).encode()).decode()


# --- protocol surface ---------------------------------------------------------

def test_requirements_are_v2_caip2():
    r = x402.requirements("best_agent", 10)
    assert r.network == "eip155:84532"          # CAIP-2, Base Sepolia default
    assert r.amount == "10000" and r.scheme == "exact"
    assert r.pay_to == PAY_TO


def test_402_challenge_carries_payment_required_header(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/search?capability=anything")
        assert r.status_code == 402
        hdr = r.headers.get("PAYMENT-REQUIRED")
        assert hdr, "402 must carry the v2 PAYMENT-REQUIRED header"
        challenge = json.loads(base64.b64decode(hdr))
        assert challenge["x402Version"] == 2
        assert challenge["accepts"][0]["network"] == "eip155:84532"
        assert challenge["accepts"][0]["payTo"] == PAY_TO
        body = r.json()["detail"]
        assert body["x402Version"] == 2
        assert body["sandbox"]["unit"] == "credits_sandbox"


def test_paid_request_end_to_end_serves_result_and_settlement(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    fac = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    from app.main import app
    with TestClient(app) as client:
        p = make_payload("best_agent", 10)
        r = client.get("/search?capability=anything",
                       headers={"PAYMENT-SIGNATURE": sig_header(p)})
        assert r.status_code == 200
        resp_hdr = r.headers.get("PAYMENT-RESPONSE")
        assert resp_hdr, "success must carry the PAYMENT-RESPONSE header"
        settled = json.loads(base64.b64decode(resp_hdr))
        assert settled["success"] is True and settled["transaction"].startswith("0x")
        assert settled["network"] == "eip155:84532"
        assert len(fac.settle_calls) == 1
    # settlement record persisted with the full identity
    from app.state import store
    rec = [b for b in store.billing_log if b.get("type") == "x402_payment"][-1]
    for field in ("facilitator", "network", "asset", "amount_atomic", "payer",
                  "recipient", "transaction", "status", "payment_identity"):
        assert rec.get(field), f"settlement record missing {field}"
    assert rec["status"] == "settled" and rec["mainnet"] is False


def test_failed_settlement_never_serves_the_result(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setattr(x402, "_facilitator",
                        lambda: FakeFacilitator(settle_ok=False))
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/search?capability=anything",
                       headers={"PAYMENT-SIGNATURE": sig_header(
                           make_payload("best_agent", 10))})
        assert r.status_code == 402
        assert r.json()["detail"]["error"] == "x402_payment_rejected"


# --- binding + replay security regressions ------------------------------------

def _expect_binding_error(payload, reason, endpoint="best_agent", cost=10):
    with pytest.raises(x402.PaymentBindingError) as e:
        x402.check_binding(payload, endpoint, cost)
    assert e.value.reason == reason


def test_replay_is_rejected(monkeypatch):
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    p = make_payload("best_agent", 10)
    assert x402.process_payment(p, "best_agent", 10)["ok"]
    with pytest.raises(x402.PaymentBindingError) as e:
        x402.process_payment(p, "best_agent", 10)
    assert e.value.reason == "replay_rejected"


def test_cross_resource_substitution_rejected():
    # payment bound to /check cannot buy /agents/{id}/reputation
    p = make_payload("best_agent", 10)
    with pytest.raises(x402.PaymentBindingError) as e:
        x402.check_binding(p, "reputation", 5)
    assert e.value.reason in ("requirements_mismatch", "resource_mismatch")


def test_price_substitution_rejected():
    offered = x402.requirements("best_agent", 10)
    cheap = offered.model_copy(update={"amount": "1"})
    _expect_binding_error(
        make_payload("best_agent", 10, accepted=cheap, amount="1"),
        "requirements_mismatch")
    # tampering ONLY the inner authorization value is also caught
    _expect_binding_error(make_payload("best_agent", 10, amount="1"),
                          "amount_mismatch")


def test_recipient_substitution_rejected():
    evil = "0x" + "99" * 20
    offered = x402.requirements("best_agent", 10)
    _expect_binding_error(
        make_payload("best_agent", 10,
                     accepted=offered.model_copy(update={"pay_to": evil}),
                     pay_to=evil),
        "requirements_mismatch")
    _expect_binding_error(make_payload("best_agent", 10, pay_to=evil),
                          "recipient_mismatch")


def test_expired_and_not_yet_valid_authorizations_rejected():
    now = time.time()
    _expect_binding_error(
        make_payload("best_agent", 10, valid_before=now - 5),
        "authorization_expired")
    _expect_binding_error(
        make_payload("best_agent", 10, valid_after=now + 3600),
        "authorization_not_yet_valid")


def test_wrong_resource_url_rejected():
    _expect_binding_error(
        make_payload("best_agent", 10, url="https://evil.example/check"),
        "resource_mismatch")


def test_wrong_version_rejected():
    _expect_binding_error(make_payload("best_agent", 10, version=3),
                          "invalid_x402_version")


def test_double_settlement_rejected_via_persisted_record(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    from app.main import app
    from app.state import store
    p = make_payload("best_agent", 10)
    with TestClient(app) as client:
        hdr = {"PAYMENT-SIGNATURE": sig_header(p)}
        assert client.get("/search?capability=x", headers=hdr).status_code == 200
        # same payment identity again — in-process replay guard
        r2 = client.get("/search?capability=x", headers=hdr)
        assert r2.status_code == 402
    # simulate a RESTART: fresh in-process guard, persisted record remains
    auth = p.payload["authorization"]
    ident = x402.replay_guard.identity(auth)
    assert store.x402_identity_settled(ident) is True
    monkeypatch.setattr(x402, "replay_guard", x402._ReplayGuard())
    with TestClient(app) as client:
        r3 = client.get("/search?capability=x",
                        headers={"PAYMENT-SIGNATURE": sig_header(p)})
        assert r3.status_code == 402
        assert r3.json()["detail"]["reason"] == "double_settlement_rejected"


# --- v1 legacy: deprecated, and cannot weaken v2 --------------------------------

def _v1_header(payload_v2: PaymentPayload, network="base-sepolia") -> str:
    v1 = {"x402Version": 1, "scheme": "exact", "network": network,
          "payload": payload_v2.payload}
    return base64.b64encode(json.dumps(v1).encode()).decode()


def test_v1_header_still_settles_but_is_deprecated(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/search?capability=x",
                       headers={"X-PAYMENT": _v1_header(make_payload("best_agent", 10))})
        assert r.status_code == 200
        assert r.headers.get("Deprecation") == "true"
    from app.state import store
    rec = [b for b in store.billing_log if b.get("type") == "x402_payment"][-1]
    assert rec["protocol"] == "v1"


def test_v1_cannot_weaken_v2_validation(monkeypatch):
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    # wrong amount through the v1 door → same guard, same rejection
    with pytest.raises(x402.PaymentBindingError) as e:
        x402.process_v1_payment_header(
            _v1_header(make_payload("best_agent", 10, amount="1")),
            "best_agent", 10)
    assert e.value.reason == "amount_mismatch"
    # wrong recipient
    with pytest.raises(x402.PaymentBindingError) as e:
        x402.process_v1_payment_header(
            _v1_header(make_payload("best_agent", 10, pay_to="0x" + "99" * 20)),
            "best_agent", 10)
    assert e.value.reason == "recipient_mismatch"
    # unknown / mismatched v1 network names
    with pytest.raises(x402.PaymentBindingError):
        x402.process_v1_payment_header(
            _v1_header(make_payload("best_agent", 10), network="fakenet"),
            "best_agent", 10)
    with pytest.raises(x402.PaymentBindingError):
        x402.process_v1_payment_header(
            _v1_header(make_payload("best_agent", 10), network="base"),
            "best_agent", 10)
    # a replayed v1 payment is caught by the shared replay guard
    p = make_payload("best_agent", 10)
    assert x402.process_v1_payment_header(_v1_header(p), "best_agent", 10)["ok"]
    with pytest.raises(x402.PaymentBindingError) as e:
        x402.process_v1_payment_header(_v1_header(p), "best_agent", 10)
    assert e.value.reason == "replay_rejected"


def test_v1_payload_rejected_on_v2_header():
    v1_on_v2 = base64.b64encode(json.dumps(
        {"x402Version": 1, "scheme": "exact", "network": "base-sepolia",
         "payload": {}}).encode()).decode()
    with pytest.raises(x402.PaymentBindingError) as e:
        x402.decode_payment_signature(v1_on_v2)
    assert e.value.reason == "invalid_x402_version"


# --- revenue honesty ------------------------------------------------------------

def test_testnet_settlement_never_counts_as_real_revenue(monkeypatch):
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    from app.state import store
    before = store.escrow_summary()
    settled = x402.process_payment(make_payload("best_agent", 10),
                                   "best_agent", 10)
    assert settled["ok"] and settled["mainnet"] is False
    store.record_x402_payment("best_agent", 10, settled)
    rev = store.escrow_summary()
    assert (rev["real_settlement"]["transactions"]
            == before["real_settlement"]["transactions"])   # unmoved
    assert (rev["real_settlement"]["revenue_usd"]
            == before["real_settlement"]["revenue_usd"])
    assert (rev["testnet_settlement"]["transactions"]
            == before["testnet_settlement"]["transactions"] + 1)


def test_only_independently_confirmed_mainnet_settlement_counts(monkeypatch):
    # facilitator settling on Base MAINNET + INDEPENDENT on-chain
    # confirmation (fake RPC receipt with the exact USDC contract,
    # recipient and amount) → counted. The facilitator response alone is
    # NEVER sufficient (tests/test_x402_cdp_settlement.py proves the
    # unconfirmed cases).
    from app import x402_confirm
    from app.state import store
    monkeypatch.setenv("GUILD_X402_NETWORK", "eip155:8453")
    monkeypatch.setenv("GUILD_X402_PAY_TO", x402.MAINNET_TREASURY)
    monkeypatch.setenv("CDP_API_KEY_ID", "test-key-id")
    from tests.test_x402_cdp_settlement import FAKE_SECRET, _receipt
    monkeypatch.setenv("CDP_API_KEY_SECRET", FAKE_SECRET)
    monkeypatch.setattr(x402, "_facilitator",
                        lambda: FakeFacilitator(network="eip155:8453"))
    monkeypatch.setattr(x402_confirm, "_get_receipt",
                        lambda tx, timeout=15.0: _receipt())
    before = store.escrow_summary()["real_settlement"]
    settled = x402.process_payment(make_payload("best_agent", 10),
                                   "best_agent", 10)
    assert settled["mainnet"] is True and settled["transaction"]
    assert settled["status"] == "settled_confirmed" and settled["confirmed"]
    store.record_x402_payment("best_agent", 10, settled)
    rev = store.escrow_summary()["real_settlement"]
    assert rev["transactions"] == before["transactions"] + 1
    assert rev["revenue_usd"] == pytest.approx(before["revenue_usd"] + 0.01)
    assert settled["transaction"] in rev["transaction_hashes"]
