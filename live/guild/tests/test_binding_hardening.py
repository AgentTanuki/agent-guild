"""Wallet-binding + caller-proof correctness hardening (P1).

  * binding.expires_at must EQUAL the stored challenge expiry exactly;
  * the DID is validated BEFORE a challenge is persisted;
  * the network must be an allowed CAIP-2 settlement network;
  * wallet lookup is exact (address, network) — the settled network decides;
  * expired challenges are garbage-collected; per-DID and global challenge
    stores are bounded;
  * rotation/supersession is deterministic: one (address, network) can never
    ambiguously represent multiple active DIDs;
  * proofs require iat < exp and a positive bounded lifetime;
  * offline cryptographic validity is SEPARATE from live revocation/status —
    a live-store lookup is never described as offline verification;
  * cached idempotent settlement records preserve attribution fields.
"""
import base64
import json
import time
import uuid

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from app import callerproof, crypto, payments, walletbinding, x402, \
    x402_confirm
from app.state import store

MAINNET = "eip155:8453"
TESTNET = "eip155:84532"
PAYER = "0x" + "22" * 20
EXT_UA = "external-agent-framework/2.0 (crewai)"


@pytest.fixture(autouse=True)
def _clean_residue():
    events_before = len(store.events)
    dedupe_before = dict(store.demand_dedupe)
    yield
    del store.events[events_before:]
    store.demand_dedupe.clear()
    store.demand_dedupe.update(dedupe_before)


def _did():
    priv, pub = crypto.generate_keypair()
    return priv, crypto.did_from_public_key(pub)


def _cred(did, address=PAYER, network=MAINNET):
    return walletbinding.issue_credential(
        store, did=did, address=address, network=network,
        challenge_nonce="h-" + uuid.uuid4().hex)


# ---------------------------------------------------------------------------
# challenge lifecycle
# ---------------------------------------------------------------------------

def test_challenge_requires_a_valid_resolvable_did():
    for bad in ("", "not-a-did", "did:web:evil.example",
                "did:key:zNotBase58!!!", "did:key:z6Mk"):
        with pytest.raises(walletbinding.BindingError):
            walletbinding.new_challenge(store, bad)
        for ch in store.wallet_binding_challenges.values():
            assert ch.get("did") != bad, (
                "an invalid DID must never persist a challenge")


def test_binding_expiry_must_equal_challenge_expiry_exactly():
    priv, did = _did()
    acct = Account.create()
    ch = walletbinding.new_challenge(store, did)
    # a later (but still future) expiry than the challenge issued — rejected
    binding = walletbinding.binding_payload(
        did=did, address=acct.address, network=MAINNET,
        nonce=ch["nonce"], expires_at="2099-01-01T00:00:00+00:00")
    did_sig = crypto.sign_jcs(binding, priv)
    evm_sig = Account.sign_message(
        encode_defunct(text=walletbinding.binding_message(binding)),
        acct.key).signature.hex()
    with pytest.raises(walletbinding.BindingError, match="expir"):
        walletbinding.verify_and_issue(store, binding, did_sig, evm_sig)


def test_network_must_be_an_allowed_caip2_settlement_network():
    priv, did = _did()
    acct = Account.create()
    for bad_net in ("eip155:1", "mainnet", "base", "eip155:999999", ""):
        ch = walletbinding.new_challenge(store, did)
        binding = walletbinding.binding_payload(
            did=did, address=acct.address, network=bad_net or "x",
            nonce=ch["nonce"], expires_at=ch["expires_at"])
        if not bad_net:
            binding["network"] = ""
        did_sig = crypto.sign_jcs(binding, priv)
        evm_sig = Account.sign_message(
            encode_defunct(text=walletbinding.binding_message(binding)),
            acct.key).signature.hex()
        with pytest.raises(walletbinding.BindingError):
            walletbinding.verify_and_issue(store, binding, did_sig, evm_sig)


def test_full_flow_still_works_on_an_allowed_network():
    priv, did = _did()
    acct = Account.create()
    ch = walletbinding.new_challenge(store, did)
    binding = walletbinding.binding_payload(
        did=did, address=acct.address, network=TESTNET,
        nonce=ch["nonce"], expires_at=ch["expires_at"])
    did_sig = crypto.sign_jcs(binding, priv)
    evm_sig = Account.sign_message(
        encode_defunct(text=walletbinding.binding_message(binding)),
        acct.key).signature.hex()
    cred = walletbinding.verify_and_issue(store, binding, did_sig, evm_sig)
    assert cred["status"] == "active"
    assert cred["network"] == TESTNET


def test_expired_challenges_are_garbage_collected():
    priv, did = _did()
    ch = walletbinding.new_challenge(store, did)
    nonce = ch["nonce"]
    # force-expire it, then issue a new challenge → GC removes the stale one
    store.wallet_binding_challenges[nonce]["expires_at"] = \
        "2000-01-01T00:00:00+00:00"
    walletbinding.new_challenge(store, did)
    assert nonce not in store.wallet_binding_challenges


def test_per_did_outstanding_challenge_limit():
    priv, did = _did()
    for _ in range(walletbinding.MAX_CHALLENGES_PER_DID):
        walletbinding.new_challenge(store, did)
    with pytest.raises(walletbinding.BindingError, match="outstanding"):
        walletbinding.new_challenge(store, did)


def test_global_challenge_store_is_bounded(monkeypatch):
    monkeypatch.setattr(walletbinding, "MAX_CHALLENGES_GLOBAL", 6)
    store.wallet_binding_challenges.clear()
    dids = []
    for _ in range(6):
        priv, did = _did()
        dids.append(did)
        walletbinding.new_challenge(store, did)
    priv, did = _did()
    with pytest.raises(walletbinding.BindingError, match="challenge store"):
        walletbinding.new_challenge(store, did)


# ---------------------------------------------------------------------------
# exact (address, network) matching + deterministic supersession
# ---------------------------------------------------------------------------

def test_wallet_lookup_is_exact_address_and_network():
    priv, did = _did()
    addr = ("0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8])[:42].lower()
    cred = _cred(did, address=addr, network=TESTNET)
    assert store.active_wallet_binding(addr, TESTNET)["credential_id"] == \
        cred["credential_id"]
    assert store.active_wallet_binding(addr, MAINNET) is None, (
        "a Base-Sepolia binding must never attribute a Base-mainnet payment")


def test_attribution_uses_the_settled_network():
    priv, did = _did()
    addr = ("0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8])[:42].lower()
    _cred(did, address=addr, network=TESTNET)
    out = payments.classify_payer_attribution(
        store, payer=addr, network=MAINNET, caller_did=did)
    assert out["class"] == "unverified_payer", (
        "network mismatch: the binding does not cover the settled network")
    out = payments.classify_payer_attribution(
        store, payer=addr, network=TESTNET, caller_did=did)
    assert out["class"] == "cryptographically_bound_machine_payer"


def test_rebinding_the_same_address_network_supersedes_deterministically():
    addr = ("0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8])[:42].lower()
    _, did_a = _did()
    _, did_b = _did()
    cred_a = _cred(did_a, address=addr, network=MAINNET)
    cred_b = _cred(did_b, address=addr, network=MAINNET)
    live_a = store.wallet_bindings[cred_a["credential_id"]]
    assert live_a["status"] == "superseded"
    assert live_a["superseded_by"] == cred_b["credential_id"]
    active = store.active_wallet_binding(addr, MAINNET)
    assert active["credential_id"] == cred_b["credential_id"]
    assert active["did"] == did_b, (
        "one (address, network) must resolve to exactly ONE active DID")


# ---------------------------------------------------------------------------
# proof lifetime
# ---------------------------------------------------------------------------

def _signed_envelope(priv, did, payload):
    return {"payload": payload,
            "signature": crypto.sign_jcs(payload, priv),
            "verificationMethod": crypto.did_key_verification_method(did)}


def _base_payload(did, now):
    return {"v": callerproof.PROTOCOL, "did": did, "method": "GET",
            "resource": "/check?capability=x",
            "body_sha256": callerproof.body_sha256(b""),
            "iat": int(now), "exp": int(now + 300),
            "nonce": uuid.uuid4().hex, "aud": callerproof.AUDIENCE}


def test_proof_requires_iat_strictly_before_exp():
    priv, did = _did()
    now = time.time()
    for iat, exp in ((int(now + 60), int(now + 60)),      # iat == exp
                     (int(now + 120), int(now + 60))):    # iat > exp
        p = _base_payload(did, now)
        p["iat"], p["exp"] = iat, exp
        out = callerproof.verify_proof(
            store, _signed_envelope(priv, did, p), method="GET",
            resource="/check?capability=x", body=b"", now=now)
        assert out["verified"] is False, f"iat={iat} exp={exp} must fail"
    p = _base_payload(did, now)
    out = callerproof.verify_proof(
        store, _signed_envelope(priv, did, p), method="GET",
        resource="/check?capability=x", body=b"", now=now)
    assert out["verified"] is True


# ---------------------------------------------------------------------------
# offline validity vs live status — never conflated
# ---------------------------------------------------------------------------

def test_offline_validity_is_separate_from_live_status():
    priv, did = _did()
    cred = _cred(did)
    assert walletbinding.credential_offline_valid(store, cred) is True
    assert walletbinding.credential_status_live(store, cred) is True
    store.revoke_wallet_binding(cred["credential_id"])
    live = store.wallet_bindings[cred["credential_id"]]
    # offline math still verifies (the signature is intact) …
    assert walletbinding.credential_offline_valid(store, cred) is True
    # … but LIVE status says revoked, and the combined check fails
    assert walletbinding.credential_status_live(store, live) is False
    assert walletbinding.verify_credential(store, cred) is False


# ---------------------------------------------------------------------------
# cached idempotent settlement preserves attribution (HTTP path)
# ---------------------------------------------------------------------------

@pytest.fixture
def _mainnet(monkeypatch):
    from tests.test_x402_v2 import FakeFacilitator
    from tests.test_x402_cdp_settlement import (FAKE_KEY_ID, FAKE_SECRET,
                                                _receipt)
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_NETWORK", MAINNET)
    monkeypatch.setenv("GUILD_X402_PAY_TO", x402.MAINNET_TREASURY)
    monkeypatch.setenv("CDP_API_KEY_ID", FAKE_KEY_ID)
    monkeypatch.setenv("CDP_API_KEY_SECRET", FAKE_SECRET)
    monkeypatch.delenv("GUILD_X402_ASSET", raising=False)
    monkeypatch.delenv("GUILD_X402_FACILITATOR", raising=False)
    monkeypatch.setenv("GUILD_X402_CONFIRM_TIMEOUT", "0")
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setattr(x402, "_facilitator",
                        lambda: FakeFacilitator(network=MAINNET))
    monkeypatch.setattr(x402_confirm, "_get_receipt",
                        lambda tx, timeout=15.0: _receipt())
    monkeypatch.setattr(x402_confirm, "current_block",
                        lambda network=None, timeout=15.0: 5_000_000)
    billing_before = len(store.billing_log)
    yield
    del store.billing_log[billing_before:]


def test_cached_idempotent_settlement_preserves_attribution(_mainnet):
    from x402.extensions.payment_identifier import PAYMENT_IDENTIFIER
    from x402.schemas import PaymentPayload
    from tests.test_x402_v2 import make_payload, sig_header
    from app.main import app
    priv, did = _did()
    _cred(did)
    cap = "cache-" + uuid.uuid4().hex[:8]
    resource = f"/check?capability={cap}"
    preq = payments.check_request(cap)
    d = make_payload(preq).model_dump(by_alias=True, exclude_none=True)
    pid = "cacheattr_" + uuid.uuid4().hex
    d["extensions"] = {PAYMENT_IDENTIFIER: {
        "info": {"required": False, "id": pid}}}
    payload = PaymentPayload(**d)
    env = callerproof.create_proof(priv, did, method="GET",
                                   resource=resource, body=b"")
    hdrs = {"User-Agent": EXT_UA,
            "PAYMENT-SIGNATURE": sig_header(payload),
            callerproof.HTTP_HEADER:
                base64.b64encode(json.dumps(env).encode()).decode()}
    with TestClient(app) as client:
        r1 = client.get(resource, headers=hdrs)
        assert r1.status_code == 200, r1.text
        # replay: same identifier + same payload, NO fresh proof header
        r2 = client.get(resource, headers={
            "User-Agent": EXT_UA, "PAYMENT-SIGNATURE": sig_header(payload)})
        assert r2.status_code == 200
        assert r2.headers.get("X-Guild-Payment-Idempotent-Replay") == "true"
    rec = store.x402_payment_id_get(pid)
    assert rec["status"] == "completed"
    settlement = rec["settlement"]
    assert settlement.get("payer_attribution") == \
        "cryptographically_bound_machine_payer", (
        "the durable cached settlement must preserve attribution")
    assert settlement.get("caller_did") == did
    assert settlement.get("wallet_binding_credential")
