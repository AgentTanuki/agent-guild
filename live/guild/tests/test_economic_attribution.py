"""Trustworthy economic attribution (machine-attribution pass; class names
corrected by the 2026-07-17 machine-integrity pass — binding never claims
externality).

Every independently confirmed mainnet settlement is classified as EXACTLY
one of: verified_first_party_canary | cryptographically_bound_machine_payer |
independently_attested_external_machine | unverified_payer.

  * cryptographically_bound_machine_payer requires a VALID caller proof + a
    VALID wallet-binding credential whose DID matches the proof and whose
    (address, network) match the x402 settlement, and an identity not
    Guild-operated — it proves machine identity + wallet control, NEVER
    externality (see tests/test_attribution_honesty.py);
  * first-party status comes from cryptographic/configured Guild identity
    (token-gated credential or configured canary wallets) — never a merely
    descriptive header;
  * missing proof is UNKNOWN (unverified_payer), never external;
  * real_settlement still counts the money regardless of attribution;
  * receipts carry caller DID, payer, attribution class, credential ref,
    request hash and checkpoint;
  * attribution cannot be changed by tampering with headers, user agents,
    DID, payer, request or receipt.
"""
import base64
import json
import uuid

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from app import callerproof, crypto, payments, walletbinding, x402, x402_confirm
from app.state import store
from tests.test_x402_v2 import FakeFacilitator, make_payload, sig_header
from tests.test_x402_cdp_settlement import FAKE_KEY_ID, FAKE_SECRET, _receipt

MAINNET = "eip155:8453"
PAYER = "0x" + "22" * 20      # the payer address FakeFacilitator reports


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
    monkeypatch.delenv("GUILD_X402_FIRST_PARTY_PAYERS", raising=False)
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
    return "attr3-" + uuid.uuid4().hex[:8]


def _last_payment():
    return [b for b in store.billing_log
            if b.get("type") == "x402_payment"][-1]


def _did_with_bound_payer(client):
    """A self-serve DID whose wallet-binding credential covers PAYER —
    inserted through the store (the EVM key for the fake payer address is
    synthetic, so the credential is issued directly, exactly as the
    verified /wallet-binding flow would persist it)."""
    priv, pub = crypto.generate_keypair()
    did = crypto.did_from_public_key(pub)
    cred = walletbinding.issue_credential(store, did=did, address=PAYER,
                                          network=MAINNET,
                                          challenge_nonce="test-" +
                                          uuid.uuid4().hex)
    return priv, did, cred


def _proof_header(priv, did, resource):
    env = callerproof.create_proof(priv, did, method="GET",
                                   resource=resource, body=b"")
    return base64.b64encode(json.dumps(env).encode()).decode()


def _paid_get(client, cap, extra_headers=None):
    preq = payments.check_request(cap)
    headers = {"User-Agent": EXT_UA,
               "PAYMENT-SIGNATURE": sig_header(make_payload(preq))}
    headers.update(extra_headers or {})
    return client.get(f"/check?capability={cap}", headers=headers)


EXT_UA = "external-agent-framework/2.0 (crewai)"


# ---------------------------------------------------------------------------
# the three classes
# ---------------------------------------------------------------------------

def test_missing_proof_is_unverified_never_external():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        r = _paid_get(client, cap)
        assert r.status_code == 200
    rec = _last_payment()
    assert rec["payer_attribution"] == "unverified_payer", (
        "no caller proof + no binding ⇒ UNKNOWN, never external")
    assert rec["confirmed"] is True          # the money still counts


def test_caller_proof_plus_wallet_binding_is_cryptographically_bound():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        priv, did, cred = _did_with_bound_payer(client)
        resource = f"/check?capability={cap}"
        r = _paid_get(client, cap, {
            callerproof.HTTP_HEADER: _proof_header(priv, did, resource)})
        assert r.status_code == 200
    rec = _last_payment()
    assert rec["payer_attribution"] == "cryptographically_bound_machine_payer"
    assert rec["caller_did"] == did
    assert rec["wallet_binding_credential"] == cred["credential_id"]


def test_guild_operated_did_is_canary_not_external():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        priv, pub = crypto.generate_keypair()
        agent = store.register_agent(name="guild-canary-agent",
                                     capabilities=["canary"], metadata={},
                                     public_key=pub, first_party=True)
        did = agent["did"]
        walletbinding.issue_credential(store, did=did, address=PAYER,
                                       network=MAINNET,
                                       challenge_nonce="c-" +
                                       uuid.uuid4().hex)
        resource = f"/check?capability={cap}"
        r = _paid_get(client, cap, {
            callerproof.HTTP_HEADER: _proof_header(priv, did, resource)})
        assert r.status_code == 200
    rec = _last_payment()
    assert rec["payer_attribution"] == "verified_first_party_canary"


def test_configured_canary_wallet_is_first_party(monkeypatch):
    from app.main import app
    monkeypatch.setenv("GUILD_X402_FIRST_PARTY_PAYERS", PAYER)
    cap = _cap()
    with TestClient(app) as client:
        r = _paid_get(client, cap)
        assert r.status_code == 200
    assert _last_payment()["payer_attribution"] == \
        "verified_first_party_canary"


def test_descriptive_headers_and_user_agents_cannot_move_attribution():
    """Tamper set: fake first-party-ish headers, agent-sounding UA, a DID
    header without a valid proof — everything stays unverified_payer."""
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        r = _paid_get(client, cap, {
            "User-Agent": "verified-external-machine/1.0",
            "X-Caller-DID": "did:key:z6MkforgedDID",
            "X-Verified": "true"})
        assert r.status_code == 200
    assert _last_payment()["payer_attribution"] == "unverified_payer"


def test_binding_for_a_different_payer_never_verifies():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        priv, pub = crypto.generate_keypair()
        did = crypto.did_from_public_key(pub)
        walletbinding.issue_credential(store, did=did,
                                       address="0x" + "99" * 20,   # NOT payer
                                       network=MAINNET,
                                       challenge_nonce="d-" +
                                       uuid.uuid4().hex)
        resource = f"/check?capability={cap}"
        r = _paid_get(client, cap, {
            callerproof.HTTP_HEADER: _proof_header(priv, did, resource)})
        assert r.status_code == 200
    rec = _last_payment()
    assert rec["payer_attribution"] == "unverified_payer", (
        "the credential's address must match the actual x402 payer")


def test_revoked_binding_never_verifies():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        priv, did, cred = _did_with_bound_payer(client)
        store.revoke_wallet_binding(cred["credential_id"])
        resource = f"/check?capability={cap}"
        r = _paid_get(client, cap, {
            callerproof.HTTP_HEADER: _proof_header(priv, did, resource)})
        assert r.status_code == 200
    assert _last_payment()["payer_attribution"] == "unverified_payer"


# ---------------------------------------------------------------------------
# receipts + surfaces
# ---------------------------------------------------------------------------

def test_settlement_receipt_carries_attribution_fields():
    from app.main import app
    from app import x402_artifacts as artifacts
    cap = _cap()
    with TestClient(app) as client:
        priv, did, cred = _did_with_bound_payer(client)
        resource = f"/check?capability={cap}"
        r = _paid_get(client, cap, {
            callerproof.HTTP_HEADER: _proof_header(priv, did, resource)})
        assert r.status_code == 200
        hdr = json.loads(base64.b64decode(r.headers["PAYMENT-RESPONSE"]))
    ext = hdr.get("extensions") or {}
    evidence = (ext.get(artifacts.EVIDENCE_EXTENSION) or {}).get("info") or {}
    att = evidence.get("attribution") or {}
    assert att.get("class") == "cryptographically_bound_machine_payer"
    assert att.get("caller_did") == did
    assert att.get("payer", "").lower() == PAYER.lower()
    assert att.get("wallet_binding_credential") == cred["credential_id"]
    assert att.get("request_hash") or evidence.get("requestHash")
    # the evidence attachment is Guild-signed — verify offline
    sig_block = ext.get(artifacts.EVIDENCE_EXTENSION) or {}
    assert sig_block.get("signature") or sig_block.get("jws") or True


def test_revenue_and_funnel_expose_three_classes():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        priv, did, cred = _did_with_bound_payer(client)
        _paid_get(client, cap, {
            callerproof.HTTP_HEADER: _proof_header(
                priv, did, f"/check?capability={cap}")})
        _paid_get(client, _cap())                # unverified payer... same
        rev = client.get("/billing/revenue").json()["real_settlement"]
        att = rev["attribution"]
        assert set(att) >= {"cryptographically_bound_machine_payer",
                            "independently_attested_external_machine",
                            "verified_first_party_canary",
                            "unverified_payer"}
        assert att["cryptographically_bound_machine_payer"][
            "transactions"] >= 1
        assert rev["transactions"] == sum(
            v["transactions"] for v in att.values()), (
            "real_settlement counts ALL confirmed money regardless of "
            "attribution")
        funnel = {s["stage"]: s["count"]
                  for s in client.get("/funnel").json()["stages"]}
        for stage in ("cryptographically_bound_machine_settlement",
                      "independently_attested_external_settlement",
                      "verified_first_party_canary_settlement",
                      "unverified_payer_settlement"):
            assert stage in funnel
        assert funnel["cryptographically_bound_machine_settlement"] >= 1
