"""x402 v2 rail: protocol surface + security regressions.

Covers, with a deterministic fake facilitator (no chain, no funds):
  * v2 challenge: HTTP 402 with the base64 PAYMENT-REQUIRED header and a
    spec-shaped body (x402Version 2, CAIP-2 network, EXACT resource object);
  * exact-resource binding — the payment binds to the trusted configured
    origin + actual method + concrete path + canonical result-affecting
    query. Path substitution, query mutation, agent-id substitution, method
    changes, hostile Host headers, price/recipient substitution, expired /
    not-yet-valid authorisations, replay and double settlement are ALL
    rejected and the protected result is never served;
  * v1 legacy (X-PAYMENT) is REJECTED on priced HTTP routes (a v1 payload
    cannot echo the resource, so it cannot be exactly bound) with a
    machine-readable migration note; the v1→v2 translation survives only for
    the A2A task-correlated path (tests/test_a2a_x402.py);
  * settlement records carry facilitator/network/asset/amount/payer/
    recipient/tx/status, and REAL revenue counts only mainnet settlements —
    testnet and mocked settlements never increase it.

The independent-official-client interoperability tests (real HTTP, official
x402 SDK clients, EVM signers) live in tests_x402_interop/ and run in their
own clean CI environment.
"""
import base64
import json
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from x402.schemas import PaymentPayload, ResourceInfo

from app import payments, x402

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

    def __init__(self, verify_ok=True, settle_ok=True, network=None,
                 raise_error=None):
        self.verify_ok, self.settle_ok = verify_ok, settle_ok
        self.network = network
        self.raise_error = raise_error
        self.verify_calls, self.settle_calls = [], []

    def verify(self, payload, requirements):
        if self.raise_error:
            raise self.raise_error
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


SEARCH = payments.search_request("anything")


def make_payload(preq=None, cost=10, *, amount=None, pay_to=None,
                 url=None, nonce=None, valid_after=None, valid_before=None,
                 accepted=None, version=2) -> PaymentPayload:
    """A structurally valid v2 exact-EVM PaymentPayload bound to `preq`,
    with keyword hooks to tamper with any bound field."""
    preq = preq or SEARCH
    offered = x402.requirements(cost)
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
        resource=ResourceInfo(url=url or preq.resource_url,
                              mime_type="application/json"),
        payload={"signature": "0x" + "ab" * 65, "authorization": auth},
    )


def sig_header(payload: PaymentPayload) -> str:
    return base64.b64encode(json.dumps(
        payload.model_dump(by_alias=True, exclude_none=True)).encode()).decode()


# --- protocol surface ---------------------------------------------------------

def test_requirements_are_v2_caip2():
    r = x402.requirements(10)
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


def test_402_resource_is_the_actual_request_not_a_template(monkeypatch):
    """THE production defect this sprint reproduces and closes:
    GET /search?capability=code-review used to answer with
    resource.url = …/check. The challenge must quote the ACTUAL semantic
    request — actual path + canonical result-affecting query."""
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/search?capability=code-review")
        assert r.status_code == 402
        challenge = json.loads(base64.b64decode(r.headers["PAYMENT-REQUIRED"]))
        url = challenge["resource"]["url"]
        assert "/search" in url and "/check" not in url
        assert "capability=code-review" in url
        # effective defaults are canonicalized in, so the binding is total
        assert "limit=20" in url and "min_trust=0" in url
        # body and header agree
        assert r.json()["detail"]["resource"]["url"] == url
        # a different concrete request quotes a different resource
        r2 = client.get("/check?capability=code-review")
        c2 = json.loads(base64.b64decode(r2.headers["PAYMENT-REQUIRED"]))
        assert c2["resource"]["url"] != url and "/check" in c2["resource"]["url"]


def test_402_resource_binds_concrete_agent_id(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    from app.main import app
    from app.state import store
    with TestClient(app) as client:
        rec = store.register_agent(name="bind-target", capabilities=["x"],
                                   metadata={})
        r = client.get(f"/agents/{rec['id']}/reputation")
        assert r.status_code == 402
        challenge = json.loads(base64.b64decode(r.headers["PAYMENT-REQUIRED"]))
        assert f"/agents/{rec['id']}/reputation" in challenge["resource"]["url"]
        assert "{id}" not in challenge["resource"]["url"]


def test_hostile_host_header_never_changes_the_resource_origin(monkeypatch):
    """The quoted origin comes ONLY from configuration, never from Host or
    forwarded headers an attacker controls."""
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_PUBLIC_HOST", "https://guild.example")
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/search?capability=x",
                       headers={"Host": "evil.example",
                                "X-Forwarded-Host": "evil.example",
                                "X-Forwarded-Proto": "http"})
        assert r.status_code in (402, 421)
        if r.status_code == 402:
            challenge = json.loads(
                base64.b64decode(r.headers["PAYMENT-REQUIRED"]))
            assert challenge["resource"]["url"].startswith(
                "https://guild.example/")


def test_paid_request_end_to_end_serves_result_and_settlement(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    fac = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    from app.main import app
    with TestClient(app) as client:
        p = make_payload(SEARCH)
        r = client.get("/search?capability=anything",
                       headers={"PAYMENT-SIGNATURE": sig_header(p)})
        assert r.status_code == 200
        resp_hdr = r.headers.get("PAYMENT-RESPONSE")
        assert resp_hdr, "success must carry the PAYMENT-RESPONSE header"
        settled = json.loads(base64.b64decode(resp_hdr))
        assert settled["success"] is True and settled["transaction"].startswith("0x")
        assert settled["network"] == "eip155:84532"
        assert len(fac.settle_calls) == 1
        # signed receipt + Guild evidence attachment ride the extensions
        exts = settled.get("extensions") or {}
        assert "offer-receipt" in exts and "io.agent-guild/evidence" in exts
    # settlement record persisted with the full identity
    from app.state import store
    rec = [b for b in store.billing_log if b.get("type") == "x402_payment"][-1]
    for field in ("facilitator", "network", "asset", "amount_atomic", "payer",
                  "recipient", "transaction", "status", "payment_identity",
                  "resource"):
        assert rec.get(field), f"settlement record missing {field}"
    assert rec["status"] == "settled" and rec["mainnet"] is False
    assert "/search" in rec["resource"]


def test_failed_settlement_never_serves_the_result(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setattr(x402, "_facilitator",
                        lambda: FakeFacilitator(settle_ok=False))
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/search?capability=anything",
                       headers={"PAYMENT-SIGNATURE": sig_header(
                           make_payload(SEARCH))})
        assert r.status_code == 402
        assert r.json()["detail"]["error"] == "x402_payment_rejected"


def test_facilitator_outage_fails_closed_and_payment_is_retryable(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setattr(
        x402, "_facilitator",
        lambda: FakeFacilitator(raise_error=ConnectionError("facilitator down")))
    from app.main import app
    p = make_payload(SEARCH)
    with TestClient(app) as client:
        r = client.get("/search?capability=anything",
                       headers={"PAYMENT-SIGNATURE": sig_header(p)})
        assert r.status_code == 402
        assert r.json()["detail"]["error"] == "x402_payment_rejected"
    # the identity was released — the SAME payment succeeds once the
    # facilitator is back (no client-side funds burned by an outage)
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    with TestClient(app) as client:
        r = client.get("/search?capability=anything",
                       headers={"PAYMENT-SIGNATURE": sig_header(p)})
        assert r.status_code == 200


# --- binding + replay security regressions ------------------------------------

def _expect_binding_error(payload, reason, preq=None, cost=10):
    with pytest.raises(x402.PaymentBindingError) as e:
        x402.check_binding(payload, preq or SEARCH, cost)
    assert e.value.reason == reason


def test_replay_is_rejected(monkeypatch):
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    p = make_payload(SEARCH)
    assert x402.process_payment(p, SEARCH, 10)["ok"]
    with pytest.raises(x402.PaymentBindingError) as e:
        x402.process_payment(p, SEARCH, 10)
    assert e.value.reason == "replay_rejected"


def test_path_substitution_rejected():
    # payment bound to /search cannot buy /check (same operation, same price,
    # DIFFERENT path)
    check = payments.check_request("anything")
    p = make_payload(SEARCH)
    with pytest.raises(x402.PaymentBindingError) as e:
        x402.check_binding(p, check, 10)
    assert e.value.reason == "resource_mismatch"


def test_query_mutation_rejected():
    # same route, one result-affecting parameter changed
    other = payments.search_request("anything", limit=1)
    p = make_payload(SEARCH)
    with pytest.raises(x402.PaymentBindingError) as e:
        x402.check_binding(p, other, 10)
    assert e.value.reason == "resource_mismatch"
    cap = payments.search_request("something-else")
    with pytest.raises(x402.PaymentBindingError) as e:
        x402.check_binding(p, cap, 10)
    assert e.value.reason == "resource_mismatch"


def test_agent_id_substitution_rejected():
    # a payment for agent A's risk-score cannot buy agent B's (same price)
    a = payments.risk_score_request("agent_aaaaaaaaaaaa")
    b = payments.risk_score_request("agent_bbbbbbbbbbbb")
    p = make_payload(a)
    with pytest.raises(x402.PaymentBindingError) as e:
        x402.check_binding(p, b, 10)
    assert e.value.reason == "resource_mismatch"


def test_method_change_rejected():
    p = make_payload(SEARCH)
    with pytest.raises(x402.PaymentBindingError) as e:
        x402.check_binding(p, SEARCH, 10, method="POST")
    assert e.value.reason == "method_mismatch"


def test_cross_operation_substitution_rejected():
    # payment bound to /search cannot buy /agents/{id}/reputation
    p = make_payload(SEARCH)
    with pytest.raises(x402.PaymentBindingError) as e:
        x402.check_binding(p, payments.reputation_request("agent_cafecafecafe"), 5)
    assert e.value.reason in ("requirements_mismatch", "resource_mismatch")


def test_payment_reuse_on_another_resource_rejected_end_to_end(monkeypatch):
    """A captured wire payment replayed against a DIFFERENT same-priced
    resource must fail on binding, and against the SAME resource must fail on
    replay/double settlement."""
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    from app.main import app
    p = make_payload(SEARCH)
    hdr = {"PAYMENT-SIGNATURE": sig_header(p)}
    with TestClient(app) as client:
        assert client.get("/search?capability=anything",
                          headers=hdr).status_code == 200
        # same price, different resource → binding failure, no settlement
        r2 = client.get("/check?capability=anything", headers=hdr)
        assert r2.status_code == 402
        assert r2.json()["detail"]["reason"] in (
            "resource_mismatch", "replay_rejected",
            "double_settlement_rejected")
        # exact same resource again → replay
        r3 = client.get("/search?capability=anything", headers=hdr)
        assert r3.status_code == 402


def test_price_substitution_rejected():
    offered = x402.requirements(10)
    cheap = offered.model_copy(update={"amount": "1"})
    _expect_binding_error(
        make_payload(SEARCH, accepted=cheap, amount="1"),
        "requirements_mismatch")
    # tampering ONLY the inner authorization value is also caught
    _expect_binding_error(make_payload(SEARCH, amount="1"),
                          "amount_mismatch")


def test_recipient_substitution_rejected():
    evil = "0x" + "99" * 20
    offered = x402.requirements(10)
    _expect_binding_error(
        make_payload(SEARCH,
                     accepted=offered.model_copy(update={"pay_to": evil}),
                     pay_to=evil),
        "requirements_mismatch")
    _expect_binding_error(make_payload(SEARCH, pay_to=evil),
                          "recipient_mismatch")


def test_wrong_network_and_asset_rejected():
    offered = x402.requirements(10)
    wrong_net = offered.model_copy(update={"network": "eip155:8453"})
    _expect_binding_error(make_payload(SEARCH, accepted=wrong_net),
                          "requirements_mismatch")
    wrong_asset = offered.model_copy(
        update={"asset": x402.USDC_BY_NETWORK["eip155:8453"]})
    _expect_binding_error(make_payload(SEARCH, accepted=wrong_asset),
                          "requirements_mismatch")


def test_expired_and_not_yet_valid_authorizations_rejected():
    now = time.time()
    _expect_binding_error(
        make_payload(SEARCH, valid_before=now - 5),
        "authorization_expired")
    _expect_binding_error(
        make_payload(SEARCH, valid_after=now + 3600),
        "authorization_not_yet_valid")


def test_wrong_resource_url_rejected():
    _expect_binding_error(
        make_payload(SEARCH, url="https://evil.example/search?capability=anything"),
        "resource_mismatch")


def test_wrong_version_rejected():
    _expect_binding_error(make_payload(SEARCH, version=3),
                          "invalid_x402_version")


def test_double_settlement_rejected_via_persisted_record(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    from app.main import app
    from app.state import store
    p = make_payload(SEARCH)
    with TestClient(app) as client:
        hdr = {"PAYMENT-SIGNATURE": sig_header(p)}
        assert client.get("/search?capability=anything",
                          headers=hdr).status_code == 200
        # same payment identity again — in-process replay guard
        r2 = client.get("/search?capability=anything", headers=hdr)
        assert r2.status_code == 402
    # simulate a RESTART: fresh in-process guard, persisted record remains
    auth = p.payload["authorization"]
    ident = x402.replay_guard.identity(auth)
    assert store.x402_identity_settled(ident) is True
    monkeypatch.setattr(x402, "replay_guard", x402._ReplayGuard())
    with TestClient(app) as client:
        r3 = client.get("/search?capability=anything",
                        headers={"PAYMENT-SIGNATURE": sig_header(p)})
        assert r3.status_code == 402
        assert r3.json()["detail"]["reason"] == "double_settlement_rejected"


# --- v1 legacy: REJECTED on priced HTTP routes ---------------------------------

def _v1_header(payload_v2: PaymentPayload, network="base-sepolia") -> str:
    v1 = {"x402Version": 1, "scheme": "exact", "network": network,
          "payload": payload_v2.payload}
    return base64.b64encode(json.dumps(v1).encode()).decode()


def test_v1_header_is_rejected_with_migration_note(monkeypatch):
    """v1 payloads carry no resource echo — they can NOT be bound to the
    exact request, so priced HTTP routes refuse them with the exact
    machine-readable upgrade path (and never serve the result)."""
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    fac = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/search?capability=x",
                       headers={"X-PAYMENT": _v1_header(make_payload(SEARCH))})
        assert r.status_code == 402
        detail = r.json()["detail"]
        assert detail["reason"] == "v1_not_accepted"
        assert "PAYMENT-SIGNATURE" in detail["detail"]
        assert fac.settle_calls == []          # nothing was settled
    from app.state import store
    assert not any(b.get("protocol") == "v1"
                   for b in store.billing_log
                   if b.get("type") == "x402_payment")


def test_v1_payload_rejected_on_v2_header():
    v1_on_v2 = base64.b64encode(json.dumps(
        {"x402Version": 1, "scheme": "exact", "network": "base-sepolia",
         "payload": {}}).encode()).decode()
    with pytest.raises(x402.PaymentBindingError) as e:
        x402.decode_payment_signature(v1_on_v2)
    assert e.value.reason == "invalid_x402_version"


def test_v1_translation_for_a2a_passes_the_same_guards(monkeypatch):
    """The v1→v2 translation kept for the A2A task path routes through the
    SAME binding guards — it cannot weaken v2 validation."""
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    p = make_payload(SEARCH, amount="1")
    v1 = {"x402Version": 1, "scheme": "exact", "network": "base-sepolia",
          "payload": p.payload}
    translated = x402.v1_payload_to_v2(v1, SEARCH, 10)
    with pytest.raises(x402.PaymentBindingError) as e:
        x402.process_payment(translated, SEARCH, 10, protocol="v1")
    assert e.value.reason == "amount_mismatch"
    # unknown / mismatched v1 network names
    with pytest.raises(x402.PaymentBindingError):
        x402.v1_payload_to_v2({**v1, "network": "fakenet"}, SEARCH, 10)
    with pytest.raises(x402.PaymentBindingError):
        x402.v1_payload_to_v2({**v1, "network": "base"}, SEARCH, 10)


# --- revenue honesty ------------------------------------------------------------

def test_testnet_settlement_never_counts_as_real_revenue(monkeypatch):
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    from app.state import store
    before = store.escrow_summary()
    settled = x402.process_payment(make_payload(SEARCH), SEARCH, 10)
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
    settled = x402.process_payment(make_payload(SEARCH), SEARCH, 10)
    assert settled["mainnet"] is True and settled["transaction"]
    assert settled["status"] == "settled_confirmed" and settled["confirmed"]
    store.record_x402_payment("best_agent", 10, settled)
    rev = store.escrow_summary()["real_settlement"]
    assert rev["transactions"] == before["transactions"] + 1
    assert rev["revenue_usd"] == pytest.approx(before["revenue_usd"] + 0.01)
    assert settled["transaction"] in rev["transaction_hashes"]
