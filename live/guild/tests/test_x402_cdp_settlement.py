"""Production x402 settlement hardening: authenticated CDP facilitator,
fail-closed mainnet configuration, and INDEPENDENT on-chain confirmation.

Everything here is deterministic — fake facilitators, fake RPC receipts,
fabricated CDP keys. No chain, no funds, no real credentials.

Covers (mission list):
  * authenticated facilitator requests (request-bound Bearer JWTs through
    the official SDK's AuthProvider hook);
  * credentials never appearing in logs or responses;
  * incorrect network/asset/facilitator combinations fail closed at startup
    and at payment time;
  * false or malformed settlement responses;
  * failed transactions;
  * wrong recipient or amount in the on-chain Transfer event;
  * duplicate transactions;
  * successful independently confirmed settlement (the ONLY thing that may
    count as real revenue);
  * testnet never counting as revenue;
  * idempotent recovery: an unconfirmed settlement can be re-presented and
    confirmed later without double charging.
"""
import base64
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from app import x402, x402_cdp, x402_confirm
from tests.test_x402_v2 import FakeFacilitator, make_payload, sig_header

# mainnet recipients are PINNED to the dedicated treasury (public address)
PAY_TO = x402.MAINNET_TREASURY
MAINNET = "eip155:8453"
MAINNET_USDC = x402.USDC_BY_NETWORK[MAINNET]
TESTNET_USDC = x402.USDC_BY_NETWORK["eip155:84532"]

# fabricated CDP API key — Ed25519 seed of 0x42s, NEVER a real credential
FAKE_KEY_ID = "11111111-2222-3333-4444-555555555555"


def _fake_secret() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(b"\x42" * 32)
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(b"\x42" * 32 + pub).decode()


FAKE_SECRET = _fake_secret()


@pytest.fixture()
def mainnet_env(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_X402_NETWORK", MAINNET)
    monkeypatch.setenv("CDP_API_KEY_ID", FAKE_KEY_ID)
    monkeypatch.setenv("CDP_API_KEY_SECRET", FAKE_SECRET)
    monkeypatch.delenv("GUILD_X402_ASSET", raising=False)
    monkeypatch.delenv("GUILD_X402_FACILITATOR", raising=False)
    monkeypatch.delenv("GUILD_X402_BASE_RPC", raising=False)
    monkeypatch.setenv("GUILD_X402_CONFIRM_TIMEOUT", "0")  # no polling waits
    yield monkeypatch


def _decode_jwt(token: str) -> tuple[dict, dict]:
    def _part(x):
        return json.loads(base64.urlsafe_b64decode(x + "=" * (-len(x) % 4)))
    h, c, _ = token.split(".")
    return _part(h), _part(c)


def _receipt(status="0x1", asset=MAINNET_USDC, to=PAY_TO, amount=10000,
             transfer_topic=x402_confirm.TRANSFER_TOPIC):
    return {"status": status, "blockNumber": "0x10", "logs": [{
        "address": asset,
        "topics": [transfer_topic,
                   "0x" + "0" * 24 + ("22" * 20),          # from (unchecked)
                   "0x" + "0" * 24 + to[2:].lower()],       # indexed to
        "data": hex(amount),
    }]}


def _serve_receipt(monkeypatch, receipt):
    monkeypatch.setattr(x402_confirm, "_get_receipt",
                        lambda tx_hash, timeout=15.0: receipt)


def _rpc_down(monkeypatch):
    def _boom(tx_hash, timeout=15.0):
        raise httpx.ConnectError("rpc unreachable")
    monkeypatch.setattr(x402_confirm, "_get_receipt", _boom)


# --- authenticated facilitator requests ---------------------------------------

def test_cdp_headers_carry_request_bound_bearer_jwts(mainnet_env):
    headers = x402_cdp.create_cdp_headers()
    for endpoint, method in (("verify", "POST"), ("settle", "POST"),
                             ("supported", "GET")):
        auth = headers[endpoint]["Authorization"]
        assert auth.startswith("Bearer ")
        hdr, claims = _decode_jwt(auth.removeprefix("Bearer "))
        assert hdr["alg"] == "EdDSA" and hdr["kid"] == FAKE_KEY_ID
        assert len(hdr["nonce"]) == 16
        assert claims["iss"] == "cdp" and claims["sub"] == FAKE_KEY_ID
        assert claims["uris"] == [
            f"{method} api.cdp.coinbase.com/platform/v2/x402/{endpoint}"]
        assert claims["exp"] - claims["nbf"] == 120
        # every call mints a FRESH single-use token
        assert (x402_cdp.create_cdp_headers()[endpoint]["Authorization"]
                != auth)


def test_sdk_client_sends_authorization_to_verify_and_settle(mainnet_env):
    from x402.http import FacilitatorConfig, HTTPFacilitatorClientSync
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("Authorization", "")))
        if request.url.path.endswith("/verify"):
            return httpx.Response(200, json={"isValid": True, "payer": "0x" + "22" * 20})
        return httpx.Response(200, json={
            "success": True, "transaction": "0x" + "ab" * 32,
            "network": MAINNET, "payer": "0x" + "22" * 20})

    client = HTTPFacilitatorClientSync(FacilitatorConfig(
        url=x402_cdp.CDP_FACILITATOR_URL,
        auth_provider=x402_cdp.auth_provider(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler))))
    p = make_payload("best_agent", 10)
    offered = x402.requirements("best_agent", 10)
    assert client.verify(p, offered).is_valid
    assert client.settle(p, offered).success
    assert len(seen) == 2
    for path, auth in seen:
        assert auth.startswith("Bearer "), f"{path} was not authenticated"
        _, claims = _decode_jwt(auth.removeprefix("Bearer "))
        expected = f"POST api.cdp.coinbase.com/platform/v2/x402/{path.rsplit('/', 1)[1]}"
        assert claims["uris"] == [expected]


def test_facilitator_factory_authenticates_cdp_but_not_testnet(mainnet_env):
    fac = x402._facilitator()
    assert fac._auth_provider is not None      # CDP host → authenticated
    mainnet_env.setenv("GUILD_X402_NETWORK", "eip155:84532")
    mainnet_env.setenv("GUILD_X402_FACILITATOR", x402.TESTNET_FACILITATOR)
    assert x402._facilitator()._auth_provider is None


def test_missing_credentials_fail_closed_not_open(mainnet_env):
    mainnet_env.delenv("CDP_API_KEY_ID")
    with pytest.raises(RuntimeError) as e:
        x402_cdp.create_cdp_headers()
    assert FAKE_SECRET not in str(e.value)
    assert "not configured" in str(e.value)


# --- credentials never leak ----------------------------------------------------

def test_credentials_never_in_logs_or_responses(mainnet_env, caplog):
    with caplog.at_level(logging.DEBUG):
        x402_cdp.create_cdp_headers()
        readiness = x402.readiness()
        errs = x402.config_errors()
        body = x402.payment_required_body("best_agent", 10)
    haystacks = [caplog.text, json.dumps(readiness), json.dumps(errs),
                 json.dumps(body)]
    for hay in haystacks:
        assert FAKE_SECRET not in hay
        assert FAKE_KEY_ID not in hay        # even the key ID stays private
    # readiness is informative WITHOUT credentials
    assert readiness["facilitator_authenticated"] is True
    assert readiness["facilitator_host"] == "api.cdp.coinbase.com"
    assert readiness["network"] == MAINNET
    assert readiness["asset"] == MAINNET_USDC
    assert readiness["recipient"] == PAY_TO
    assert readiness["config_valid"] is True


def test_startup_failure_message_carries_no_secret(mainnet_env):
    mainnet_env.setenv("GUILD_X402_FACILITATOR", x402.TESTNET_FACILITATOR)
    with pytest.raises(RuntimeError) as e:
        x402.assert_config_valid()
    assert FAKE_SECRET not in str(e.value) and FAKE_KEY_ID not in str(e.value)


# --- incorrect network/asset/facilitator combinations ---------------------------

@pytest.mark.parametrize("mutate,needle", [
    (lambda m: m.setenv("GUILD_X402_FACILITATOR", x402.TESTNET_FACILITATOR),
     "testnet-only"),
    (lambda m: m.setenv("GUILD_X402_ASSET", TESTNET_USDC), "TESTNET USDC"),
    (lambda m: m.setenv("GUILD_X402_ASSET", "0x" + "99" * 20), "must be Base USDC"),
    (lambda m: m.delenv("CDP_API_KEY_SECRET"), "CDP_API_KEY_ID"),
    (lambda m: m.setenv("GUILD_X402_PAY_TO", "0x1234"), "valid non-zero EVM"),
    (lambda m: m.setenv("GUILD_X402_PAY_TO", "0x" + "00" * 20), "valid non-zero EVM"),
    # a VALID address that is not the treasury must still fail closed
    (lambda m: m.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20),
     "PINNED to the agent-guild-treasury"),
    (lambda m: m.setenv("GUILD_PUBLIC_HOST", "http://agent-guild.example"),
     "must be a valid https origin"),
    (lambda m: m.setenv("GUILD_PUBLIC_HOST", "https://127.0.0.1:8000"),
     "local/private"),
    (lambda m: m.setenv("GUILD_X402_BASE_RPC", "http://mainnet.base.org"),
     "https JSON-RPC"),
])
def test_mainnet_misconfigurations_fail_closed(mainnet_env, mutate, needle):
    assert x402.config_errors() == []        # valid before mutation
    mutate(mainnet_env)
    errs = x402.config_errors()
    assert errs and any(needle in e for e in errs), errs
    # payment time fails closed too — the facilitator is never contacted
    with pytest.raises(x402.PaymentBindingError) as e:
        x402.process_payment(make_payload("best_agent", 10), "best_agent", 10)
    assert e.value.reason == "x402_misconfigured"


def test_testnet_default_configuration_stays_valid(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    assert x402.config_errors() == []
    assert x402.facilitator_url() == x402.TESTNET_FACILITATOR
    assert x402.asset() == TESTNET_USDC


def test_mainnet_defaults_select_cdp_and_mainnet_usdc(mainnet_env):
    assert x402.facilitator_url() == x402_cdp.CDP_FACILITATOR_URL
    assert x402.asset() == MAINNET_USDC


# --- settlement responses: false, malformed, failed, wrong, duplicate ----------

def test_success_without_tx_hash_is_malformed_and_fails(mainnet_env, monkeypatch):
    class NoTxFacilitator(FakeFacilitator):
        def settle(self, payload, requirements):
            from types import SimpleNamespace
            return SimpleNamespace(success=True, error_reason=None,
                                   transaction="", network=MAINNET,
                                   payer="0x" + "22" * 20)
    monkeypatch.setattr(x402, "_facilitator", lambda: NoTxFacilitator())
    out = x402.process_payment(make_payload("best_agent", 10), "best_agent", 10)
    assert out["ok"] is False
    assert "without a valid tx hash" in out["status"]


def test_facilitator_success_alone_is_never_enough(mainnet_env, monkeypatch):
    """RPC unavailable → INDEPENDENT confirmation unavailable → fail closed."""
    monkeypatch.setattr(x402, "_facilitator",
                        lambda: FakeFacilitator(network=MAINNET))
    _rpc_down(monkeypatch)
    out = x402.process_payment(make_payload("best_agent", 10), "best_agent", 10)
    assert out["ok"] is False and out["status"] == "settled_unconfirmed"
    assert out["confirmed"] is False
    assert "rpc_unavailable" in out["confirmation"]["reason"]


def test_failed_onchain_transaction_is_not_confirmed(mainnet_env, monkeypatch):
    monkeypatch.setattr(x402, "_facilitator",
                        lambda: FakeFacilitator(network=MAINNET))
    _serve_receipt(monkeypatch, _receipt(status="0x0"))
    out = x402.process_payment(make_payload("best_agent", 10), "best_agent", 10)
    assert out["ok"] is False and out["status"] == "settled_unconfirmed"
    assert "transaction failed" in out["confirmation"]["reason"]


@pytest.mark.parametrize("receipt,reason_needle", [
    (_receipt(to="0x" + "99" * 20), "no Transfer event"),        # wrong recipient
    (_receipt(amount=1), "amount mismatch"),                     # wrong amount
    (_receipt(asset=TESTNET_USDC), "no Transfer event"),         # wrong contract
    (_receipt(transfer_topic="0x" + "aa" * 32), "no Transfer event"),
    ({"status": "0x1", "logs": []}, "no Transfer event"),
    ({}, "transaction failed"),                                  # malformed receipt
])
def test_wrong_recipient_amount_or_contract_is_not_confirmed(
        mainnet_env, monkeypatch, receipt, reason_needle):
    monkeypatch.setattr(x402, "_facilitator",
                        lambda: FakeFacilitator(network=MAINNET))
    _serve_receipt(monkeypatch, receipt)
    out = x402.process_payment(make_payload("best_agent", 10), "best_agent", 10)
    assert out["ok"] is False and out["confirmed"] is False
    assert reason_needle in out["confirmation"]["reason"]


def test_duplicate_transaction_never_buys_twice(mainnet_env, monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    fixed_tx = "0x" + "cd" * 32

    class SameTxFacilitator(FakeFacilitator):
        def settle(self, payload, requirements):
            from types import SimpleNamespace
            return SimpleNamespace(success=True, error_reason=None,
                                   transaction=fixed_tx, network=MAINNET,
                                   payer="0x" + "22" * 20)
    monkeypatch.setattr(x402, "_facilitator", lambda: SameTxFacilitator())
    _serve_receipt(monkeypatch, _receipt())
    from app.main import app
    with TestClient(app) as client:
        r1 = client.get("/search?capability=x",
                        headers={"PAYMENT-SIGNATURE": sig_header(
                            make_payload("best_agent", 10))})
        assert r1.status_code == 200
        # different payment identity, same claimed on-chain transaction
        r2 = client.get("/search?capability=x",
                        headers={"PAYMENT-SIGNATURE": sig_header(
                            make_payload("best_agent", 10))})
        assert r2.status_code == 402
        assert r2.json()["detail"]["reason"] == "duplicate_transaction"


# --- the only thing that counts: independently confirmed mainnet settlement ----

def test_confirmed_mainnet_settlement_serves_and_counts(mainnet_env, monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setattr(x402, "_facilitator",
                        lambda: FakeFacilitator(network=MAINNET))
    _serve_receipt(monkeypatch, _receipt())
    from app.main import app
    from app.state import store
    before = store.escrow_summary()["real_settlement"]
    with TestClient(app) as client:
        r = client.get("/search?capability=x",
                       headers={"PAYMENT-SIGNATURE": sig_header(
                           make_payload("best_agent", 10))})
        assert r.status_code == 200
        receipt_hdr = json.loads(base64.b64decode(r.headers["PAYMENT-RESPONSE"]))
        assert receipt_hdr["success"] is True
        assert receipt_hdr["network"] == MAINNET
    rec = [b for b in store.billing_log if b.get("type") == "x402_payment"][-1]
    assert rec["status"] == "settled_confirmed" and rec["confirmed"] is True
    after = store.escrow_summary()["real_settlement"]
    assert after["transactions"] == before["transactions"] + 1
    assert after["revenue_usd"] == pytest.approx(before["revenue_usd"] + 0.01)


def test_unconfirmed_then_recovered_is_idempotent(mainnet_env, monkeypatch):
    """RPC outage: paid-but-unconfirmed → 402 (no result, no revenue). The
    SAME payment re-presented after the RPC returns → confirmed, served,
    counted exactly once. A third presentation → double-settlement reject."""
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setattr(x402, "_facilitator",
                        lambda: FakeFacilitator(network=MAINNET))
    from app.main import app
    from app.state import store
    before = store.escrow_summary()["real_settlement"]
    p = make_payload("best_agent", 10)
    hdr = {"PAYMENT-SIGNATURE": sig_header(p)}
    with TestClient(app) as client:
        _rpc_down(monkeypatch)
        r1 = client.get("/search?capability=x", headers=hdr)
        assert r1.status_code == 402
        d = r1.json()["detail"]
        assert d["reason"] == "settlement_unconfirmed"
        assert d["transaction"].startswith("0x")
        mid = store.escrow_summary()["real_settlement"]
        assert mid["transactions"] == before["transactions"]  # no revenue
        # RPC comes back; simulate a RESTART of the in-process guard too
        monkeypatch.setattr(x402, "replay_guard", x402._ReplayGuard())
        _serve_receipt(monkeypatch, _receipt())
        r2 = client.get("/search?capability=x", headers=hdr)
        assert r2.status_code == 200
        r3 = client.get("/search?capability=x", headers=hdr)
        assert r3.status_code == 402
        assert r3.json()["detail"]["reason"] == "double_settlement_rejected"
    after = store.escrow_summary()["real_settlement"]
    assert after["transactions"] == before["transactions"] + 1   # exactly once


def test_startup_fails_closed_on_misconfigured_mainnet(mainnet_env):
    """A misconfigured MAINNET rail must refuse to BOOT — the app lifespan
    raises before serving a single request."""
    mainnet_env.setenv("GUILD_X402_FACILITATOR", x402.TESTNET_FACILITATOR)
    from app.main import app
    with pytest.raises(RuntimeError) as e:
        with TestClient(app):
            pass
    assert "testnet-only" in str(e.value)
    assert FAKE_SECRET not in str(e.value)


def test_readiness_endpoint_is_public_and_secretless(mainnet_env):
    from app.main import app
    with TestClient(app) as client:
        body = client.get("/x402/readiness")
        assert body.status_code == 200
        text = body.text
        assert FAKE_SECRET not in text and FAKE_KEY_ID not in text
        j = body.json()
        assert j["config_valid"] is True and j["mainnet"] is True
        assert j["facilitator_host"] == "api.cdp.coinbase.com"
        assert j["recipient"] == x402.MAINNET_TREASURY
        assert j["recipient_is_pinned_treasury"] is True


# --- preflight script (secret-silent credential loading) ------------------------

def _preflight_module():
    import importlib.util
    import pathlib
    path = (pathlib.Path(__file__).resolve().parents[2]
            / "scripts" / "x402_preflight.py")
    spec = importlib.util.spec_from_file_location("x402_preflight", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_preflight_loads_portal_key_file_without_printing(tmp_path, monkeypatch,
                                                          capsys):
    monkeypatch.delenv("CDP_API_KEY_ID", raising=False)
    monkeypatch.delenv("CDP_API_KEY_SECRET", raising=False)
    key_file = tmp_path / "cdp_api_key.json"
    key_file.write_text(json.dumps({"id": FAKE_KEY_ID,
                                    "privateKey": FAKE_SECRET}))
    pf = _preflight_module()
    assert pf.load_credentials(str(key_file)) is True
    import os
    assert os.environ["CDP_API_KEY_ID"] == FAKE_KEY_ID
    out = capsys.readouterr()
    assert FAKE_SECRET not in out.out + out.err
    assert FAKE_KEY_ID not in out.out + out.err
    # generated headers work off the loaded credentials
    assert x402_cdp.create_cdp_headers()["verify"]["Authorization"].startswith("Bearer ")


def test_preflight_rejects_malformed_key_file(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CDP_API_KEY_ID", raising=False)
    monkeypatch.delenv("CDP_API_KEY_SECRET", raising=False)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"something": "else"}))
    pf = _preflight_module()
    assert pf.load_credentials(str(bad)) is False
    assert "privateKey" in capsys.readouterr().out   # explains shape, no secrets
