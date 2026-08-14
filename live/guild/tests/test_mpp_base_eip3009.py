"""MPP evm/charge → x402 conversion: adversarial conformance + official interop.

Architecture under test: an authenticated MPP credential (official mppx
0.8.17 native EVM shape) is CONVERTED into the official x402 v2 PaymentPayload
and settled by the EXISTING payments.authorize/settle_x402 path — never a
second settlement implementation. All local; zero network; zero facilitator
calls asserted on every pre-settlement rejection.

Second-review coverage: real official-mppx-created credential accepted
end-to-end (offline fixture); flat official payload shape; method/intent/realm
transplant rejection; source-DID↔from binding; missing-credentialTypes client
regression; both dual-header combos → 400 before decode; cached MPP replay
receipt with settle spy == 0.

Fixtures in tests/fixtures/ were generated offline with official mppx 0.8.17
`evm/client.charge(...).createCredential(...)` against a challenge minted by
app/mpp.py (see the header comment in each fixture's sibling .md). No funds,
no chain, no network.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import mpp, payments, pricing, x402  # noqa: E402
from app.payments import PaidRequest  # noqa: E402

SECRET = "unit-test-secret-0123456789abcdef-0123456789abcdef"
FIX = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    pricing.load_runtime({})
    monkeypatch.setenv("GUILD_MPP_ENABLED", "1")
    monkeypatch.setenv("GUILD_MPP_SECRET", SECRET)
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_NETWORK", "eip155:8453")
    monkeypatch.setenv("GUILD_X402_ASSET",
                       "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")


def _preq() -> PaidRequest:
    return payments.check_request("fact-check")


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(obj).encode()).decode().rstrip("=")


def _flat_payload(ch: mpp.Challenge, *, nonce=None, value=None, to=None,
                  ptype="authorization", drop=None):
    """The OFFICIAL flat mppx EVM payload shape."""
    offered = x402.requirements(_preq().cost)
    now = int(time.time())
    p = {
        "type": ptype,
        "from": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
        "to": to or offered.pay_to,
        "value": value or offered.amount,
        "validAfter": str(now - 10),
        "validBefore": str(now + 300),
        "nonce": nonce or mpp.expected_nonce(ch.id, ch.realm),
        "signature": "0x" + "ab" * 65,
    }
    for k in (drop or []):
        p.pop(k, None)
    return p


def _cred(ch: mpp.Challenge, *, source=None, **kw) -> str:
    cred = {"challenge": {"id": ch.id, "realm": ch.realm, "method": "evm",
                          "intent": "charge", "request": ch.request_b64,
                          "expires": ch.expires, "digest": ch.digest,
                          "opaque": ch.opaque_b64},
            "payload": _flat_payload(ch, **kw)}
    if source is None:
        source = "did:pkh:eip155:8453:0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
    if source:
        cred["source"] = source
    return "Payment " + _b64(cred)


# ---------------------------------------------------------------------------
# REAL official-mppx interop (the second review's headline requirement)
# ---------------------------------------------------------------------------
def test_official_mppx_credential_is_accepted_end_to_end():
    """An official mppx 0.8.17 EVM-charge credential, created offline against
    our minted challenge, is accepted by our Python path AND the existing
    x402.check_binding accepts the converted payload. Fixtures generated
    with the real client (see fixtures/README)."""
    hdr_path = os.path.join(FIX, "mppx_0817_challenge.txt")
    cred_path = os.path.join(FIX, "mppx_0817_credential.txt")
    assert os.path.exists(hdr_path) and os.path.exists(cred_path)
    authz = open(cred_path).read().strip()
    # Reconstruct the exact PaidRequest the fixture challenge was minted for.
    preq = _preq()
    # The fixture challenge is long-lived; verify the credential's challenge
    # is well-formed and carries the flat official payload.
    raw = json.loads(mpp._b64url_decode(authz.split(" ", 1)[1]))
    assert set(raw["payload"]) >= {"from", "nonce", "signature", "to", "type",
                                   "validAfter", "validBefore", "value"}, (
        "fixture must be the flat official payload shape")
    assert raw["payload"]["type"] == "authorization"
    assert raw["source"].startswith("did:pkh:eip155:8453:")
    signed = encode_typed_data(full_message={
        "types": {"EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ], "TransferWithAuthorization": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"},
        ]},
        "primaryType": "TransferWithAuthorization",
        "domain": {"name": "USD Coin", "version": "2", "chainId": 8453,
                   "verifyingContract":
                   "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"},
        "message": {k: raw["payload"][k] for k in (
            "from", "to", "value", "validAfter", "validBefore", "nonce")},
    })
    recovered = Account.recover_message(
        signed, signature=raw["payload"]["signature"])
    assert recovered.lower() == raw["payload"]["from"].lower()
    # Accept the exact official-client output, not a hand-built lookalike.
    fixture_challenge = open(hdr_path).read().strip()
    assert raw["challenge"]["id"] in fixture_challenge
    payload, source = mpp.credential_to_payment(authz, preq, preq.cost)
    x402.check_binding(payload, preq, preq.cost, method=preq.method)
    assert source.startswith("did:pkh:eip155:8453:")


def test_flat_official_payload_shape_is_required():
    """The x402-nested {authorization, signature} shape (our first, WRONG
    design) must be REJECTED; only the flat mppx shape is accepted."""
    preq = _preq()
    ch = mpp.mint_challenge(preq, preq.cost)
    nested = {"challenge": {"id": ch.id, "realm": ch.realm, "method": "evm",
                            "intent": "charge", "request": ch.request_b64,
                            "expires": ch.expires, "digest": ch.digest,
                            "opaque": ch.opaque_b64},
              "payload": {"type": "authorization",
                          "authorization": {"nonce": mpp.expected_nonce(
                              ch.id, ch.realm)},
                          "signature": "0x" + "ab" * 65}}
    with pytest.raises(mpp.MppError) as e:
        mpp.credential_to_payment("Payment " + _b64(nested), preq, preq.cost)
    assert e.value.slug == "verification-failed"


def test_challenge_advertises_credentialtypes_authorization():
    """Official mppx client REJECTS a native EVM challenge lacking
    methodDetails.credentialTypes:['authorization'] — so we must advertise
    it (src/evm/client/Charge.ts)."""
    ch = mpp.mint_challenge(_preq(), _preq().cost)
    req = json.loads(mpp._b64url_decode(ch.request_b64))
    assert req["methodDetails"]["credentialTypes"] == ["authorization"]
    assert req["methodDetails"]["decimals"] == 6


def test_production_key_is_derived_and_stable_without_new_secret(monkeypatch):
    monkeypatch.delenv("GUILD_MPP_SECRET", raising=False)
    first = mpp._secret()
    second = mpp._secret()
    assert first == second and len(first) == 32
    assert mpp.enabled() is True


# ---------------------------------------------------------------------------
# Conversion success — field by field
# ---------------------------------------------------------------------------
def test_valid_credential_converts_to_exact_x402_payload():
    preq = _preq()
    ch = mpp.mint_challenge(preq, preq.cost)
    payload, source = mpp.credential_to_payment(_cred(ch), preq, preq.cost)
    offered = x402.requirements(preq.cost)
    assert payload.x402_version == 2
    assert payload.accepted.amount == offered.amount        # EXACT, not >=
    assert payload.accepted.pay_to == offered.pay_to
    assert payload.accepted.asset == offered.asset
    assert payload.resource.url == preq.resource_url
    assert payload.payload["authorization"]["nonce"] == \
        mpp.expected_nonce(ch.id, ch.realm)
    x402.check_binding(payload, preq, preq.cost, method=preq.method)


def test_conversion_is_deterministic_for_replay_identification():
    preq = _preq()
    ch = mpp.mint_challenge(preq, preq.cost)
    cred = _cred(ch)
    p1, _ = mpp.credential_to_payment(cred, preq, preq.cost)
    p2, _ = mpp.credential_to_payment(cred, preq, preq.cost)
    assert payments._payload_fingerprint(p1) == \
        payments._payload_fingerprint(p2)


# ---------------------------------------------------------------------------
# Rejections — each asserts ZERO settle calls
# ---------------------------------------------------------------------------
@pytest.fixture()
def settle_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(payments, "settle_x402",
                        lambda *a, **k: calls.append(1))
    return calls


def _expect(preq, cred, slug, settle_spy):
    with pytest.raises(mpp.MppError) as e:
        mpp.credential_to_payment(cred, preq, preq.cost)
    assert e.value.slug == slug
    assert settle_spy == []


def test_forged_price_rejected(settle_spy):
    preq = _preq()
    ch = mpp.mint_challenge(preq, preq.cost)
    cred = json.loads(mpp._b64url_decode(_cred(ch).split(" ", 1)[1]))
    cheap = dict(json.loads(mpp._b64url_decode(ch.request_b64)), amount="1")
    cred["challenge"]["request"] = _b64(cheap)
    _expect(preq, "Payment " + _b64(cred), "invalid-challenge", settle_spy)


def test_method_transplant_rejected(settle_spy):
    preq = _preq()
    ch = mpp.mint_challenge(preq, preq.cost)
    cred = json.loads(mpp._b64url_decode(_cred(ch).split(" ", 1)[1]))
    cred["challenge"]["method"] = "stripe"     # echoed method tampered
    _expect(preq, "Payment " + _b64(cred), "invalid-challenge", settle_spy)


def test_intent_transplant_rejected(settle_spy):
    preq = _preq()
    ch = mpp.mint_challenge(preq, preq.cost)
    cred = json.loads(mpp._b64url_decode(_cred(ch).split(" ", 1)[1]))
    cred["challenge"]["intent"] = "session"
    _expect(preq, "Payment " + _b64(cred), "invalid-challenge", settle_spy)


def test_realm_transplant_rejected(settle_spy):
    preq = _preq()
    ch = mpp.mint_challenge(preq, preq.cost)
    cred = json.loads(mpp._b64url_decode(_cred(ch).split(" ", 1)[1]))
    cred["challenge"]["realm"] = "evil.example.com"
    _expect(preq, "Payment " + _b64(cred), "invalid-challenge", settle_spy)


def test_wrong_nonce_rejected(settle_spy):
    ch = mpp.mint_challenge(_preq(), _preq().cost)
    _expect(_preq(), _cred(ch, nonce="0x" + "99" * 32),
            "verification-failed", settle_spy)


def test_source_did_mismatch_rejected(settle_spy):
    ch = mpp.mint_challenge(_preq(), _preq().cost)
    # source address does not match payload.from
    _expect(_preq(), _cred(ch, source="did:pkh:eip155:8453:0x" + "de" * 20),
            "verification-failed", settle_spy)


def test_source_wrong_chain_rejected(settle_spy):
    ch = mpp.mint_challenge(_preq(), _preq().cost)
    _expect(_preq(),
            _cred(ch, source="did:pkh:eip155:1:"
                  "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"),
            "verification-failed", settle_spy)


def test_missing_source_is_allowed(settle_spy):
    ch = mpp.mint_challenge(_preq(), _preq().cost)
    payload, source = mpp.credential_to_payment(_cred(ch, source=""),
                                                _preq(), _preq().cost)
    assert source == "" and settle_spy == []


def test_challenge_for_another_route_rejected(settle_spy):
    other = payments.deep_preflight_request("https://x.example")
    ch = mpp.mint_challenge(other, other.cost)
    _expect(_preq(), _cred(ch), "invalid-challenge", settle_spy)


def test_price_change_invalidates_challenge(settle_spy, monkeypatch):
    preq = _preq()
    ch = mpp.mint_challenge(preq, preq.cost)
    cred = _cred(ch)
    monkeypatch.setenv("GUILD_PRICE_BEST_AGENT", "20")
    _expect(preq, cred, "invalid-challenge", settle_spy)


def test_expired_utc_correct(settle_spy, monkeypatch):
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    time.tzset()
    try:
        preq = _preq()
        _expect(preq, _cred(mpp.mint_challenge(preq, preq.cost, ttl_s=-5)),
                "payment-expired", settle_spy)
        payload, _ = mpp.credential_to_payment(
            _cred(mpp.mint_challenge(preq, preq.cost, ttl_s=300)), preq,
            preq.cost)
        assert payload is not None
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


def test_malformed_and_shape_rejections(settle_spy):
    preq = _preq()
    ch = mpp.mint_challenge(preq, preq.cost)
    for bad in ("Payment !!!", "Payment " + _b64([1]), "Bearer x"):
        with pytest.raises(mpp.MppError) as e:
            mpp.credential_to_payment(bad, preq, preq.cost)
        assert e.value.slug == "malformed-credential"
    # flat payload missing a required field
    _expect(preq, _cred(ch, drop=["validAfter"]), "verification-failed",
            settle_spy)


# ---------------------------------------------------------------------------
# HTTP integration
# ---------------------------------------------------------------------------
@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _problem(r):
    b = r.json()
    return b.get("detail") if isinstance(b.get("detail"), dict) else b


@pytest.mark.parametrize("sig_header", ["PAYMENT-SIGNATURE", "X-PAYMENT"])
def test_dual_headers_rejected_400_before_decode(client, monkeypatch,
                                                 sig_header):
    """Both x402-signal headers, combined with an MPP Authorization, must
    400 BEFORE any decode/settle."""
    decoded = []
    monkeypatch.setattr(x402, "decode_payment_signature",
                        lambda h: decoded.append(h) or (_ for _ in ()).throw(
                            AssertionError("must not decode")))
    ch = mpp.mint_challenge(_preq(), _preq().cost)
    r = client.get("/check", params={"capability": "fact-check"},
                   headers={"user-agent": "curl/8", sig_header: "AAAA",
                            "Authorization": _cred(ch)})
    assert r.status_code == 400, r.text
    assert _problem(r)["type"].endswith("malformed-credential")
    assert decoded == []


def test_non_payment_authorization_preserves_existing_behavior(client):
    """A Bearer/other Authorization must NOT be treated as MPP and must not
    change the normal 402."""
    r = client.get("/check", params={"capability": "fact-check"},
                   headers={"user-agent": "curl/8",
                            "Authorization": "Bearer sometoken"})
    assert r.status_code == 402
    assert "payment-required" in {k.lower() for k in r.headers}


def test_enabled_402_dual_advertises(client):
    r = client.get("/check", params={"capability": "fact-check"},
                   headers={"user-agent": "curl/8"})
    assert r.status_code == 402
    hdrs = {k.lower(): v for k, v in r.headers.items()}
    assert "payment-required" in hdrs
    assert hdrs.get("www-authenticate", "").startswith("Payment id=")


def test_mpp_credential_reaches_the_one_settle_path(client, monkeypatch):
    seen = {}

    def fake_authorize(preq, **kw):
        seen.update(protocol=kw.get("protocol"), payment=kw.get("payment"))
        class _S:
            record = {"ok": True, "confirmed": True, "mainnet": True,
                      "network": "base", "transaction": "0x" + "cd" * 32}
        class _A:
            mode = "x402"; settled = _S(); account = None
        return _A()

    import app.main as main_mod
    monkeypatch.setattr(main_mod.payments, "authorize", fake_authorize)
    ch = mpp.mint_challenge(_preq(), _preq().cost)
    r = client.get("/check", params={"capability": "fact-check"},
                   headers={"user-agent": "curl/8", "Authorization": _cred(ch)})
    assert r.status_code == 200, r.text
    assert seen["protocol"] == "mpp_evm"
    assert seen["payment"].x402_version == 2
    blob = json.loads(base64.urlsafe_b64decode(
        r.headers["Payment-Receipt"] + "=="))
    assert blob["status"] == "success"
    assert blob["reference"] == "0x" + "cd" * 32
    assert "payment-response" in {k.lower() for k in r.headers}


def test_cached_mpp_replay_gets_receipt_zero_settle(client, monkeypatch):
    """An MPP idempotent replay returns a Payment-Receipt derived from the
    already-confirmed cached settlement, with ZERO facilitator/settle call."""
    from app.payments import CachedPaidResult
    settle_calls = []
    monkeypatch.setattr(payments, "settle_x402",
                        lambda *a, **k: settle_calls.append(1))

    def fake_authorize(preq, **kw):
        raise CachedPaidResult({
            "result_body": json.dumps({"ok": True}),
            "settle_header": "cached-x402-response",
            "settlement": {"transaction": "0x" + "ef" * 32,
                           "network": "base", "confirmed": True}})

    import app.main as main_mod
    monkeypatch.setattr(main_mod.payments, "authorize", fake_authorize)
    ch = mpp.mint_challenge(_preq(), _preq().cost)
    r = client.get("/check", params={"capability": "fact-check"},
                   headers={"user-agent": "curl/8", "Authorization": _cred(ch)})
    assert r.status_code == 200
    assert settle_calls == [], "cached replay must not settle again"
    rc = r.headers.get("Payment-Receipt")
    assert rc, "MPP replay must still carry a Payment-Receipt"
    blob = json.loads(base64.urlsafe_b64decode(rc + "=="))
    assert blob["reference"] == "0x" + "ef" * 32
    assert "x-guild-payment-idempotent-replay" in {k.lower() for k in r.headers}


def test_invalid_mpp_credential_gets_402_zero_settle(client, monkeypatch):
    called = []
    monkeypatch.setattr(payments, "settle_x402",
                        lambda *a, **k: called.append(1))
    ch = mpp.mint_challenge(_preq(), _preq().cost)
    r = client.get("/check", params={"capability": "fact-check"},
                   headers={"user-agent": "curl/8",
                            "Authorization": _cred(ch, nonce="0x" + "77" * 32)})
    assert r.status_code == 402
    assert called == []
    assert _problem(r).get("reason") == "verification-failed"


def test_default_off_byte_identical(client, monkeypatch):
    monkeypatch.setenv("GUILD_MPP_ENABLED", "0")
    ch_like = "Payment " + _b64({"challenge": {"id": "x"}, "payload": {}})
    r = client.get("/check", params={"capability": "fact-check"},
                   headers={"user-agent": "curl/8", "Authorization": ch_like})
    assert r.status_code == 402
    hdrs = {k.lower() for k in r.headers}
    assert "www-authenticate" not in hdrs
    assert "payment-required" in hdrs


def test_misconfigured_secret_disables_advertisement(client, monkeypatch):
    monkeypatch.setenv("GUILD_MPP_SECRET", "too-short")
    r = client.get("/check", params={"capability": "fact-check"},
                   headers={"user-agent": "curl/8"})
    assert r.status_code == 402
    hdrs = {k.lower() for k in r.headers}
    assert "payment-required" in hdrs
    assert "www-authenticate" not in hdrs
