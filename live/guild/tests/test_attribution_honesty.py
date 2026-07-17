"""Honest economic attribution semantics (machine-integrity correction).

The defect this closes: a self-created did:key plus a self-controlled wallet
proves machine identity continuity and wallet control. It does NOT prove the
payer is external to Agent Guild — anyone (including an Agent Guild-controlled
process) can mint a fresh DID, bind a fresh wallet and pay. The old
`verified_external_machine` class claimed externality from exactly that
evidence, so it was a self-mintable "external revenue" label.

Conservative classes (EXACTLY one per confirmed settlement):

  * verified_first_party_canary          — cryptographic/configured Guild
                                           identity; never external revenue;
  * cryptographically_bound_machine_payer — VALID caller proof + exact
                                           (address, network) wallet binding;
                                           machine identity + wallet control
                                           proven, ownership/externality
                                           UNPROVEN;
  * independently_attested_external_machine — ONLY when a separate
                                           allowlisted issuer credential
                                           explicitly establishes
                                           externality. Not yet issued by
                                           anyone ⇒ honestly zero;
  * unverified_payer                     — everything else (missing proof is
                                           UNKNOWN, never external).

Revenue: real_settlement still counts all independently confirmed money;
`cryptographically_bound_machine_revenue_usd` and
`independently_attested_external_revenue_usd` are exposed separately; no
surface may call unknown ownership "verified external".
"""
import base64
import json
import time
import uuid
from types import SimpleNamespace

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from app import callerproof, crypto, externality, payments, walletbinding, \
    x402, x402_confirm
from app.state import store
from tests.test_x402_v2 import FakeFacilitator, make_payload, sig_header
from tests.test_x402_cdp_settlement import FAKE_KEY_ID, FAKE_SECRET, _receipt

MAINNET = "eip155:8453"
PAYER = "0x" + "22" * 20      # the payer address FakeFacilitator reports
EXT_UA = "external-agent-framework/2.0 (crewai)"

CLASSES = {"verified_first_party_canary",
           "cryptographically_bound_machine_payer",
           "independently_attested_external_machine",
           "unverified_payer"}


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
    monkeypatch.delenv("GUILD_EXTERNALITY_ATTESTOR_DIDS", raising=False)
    monkeypatch.setattr(x402, "_facilitator",
                        lambda: FakeFacilitator(network=MAINNET))
    monkeypatch.setattr(x402_confirm, "_get_receipt",
                        lambda tx, timeout=15.0: _receipt())
    monkeypatch.setattr(x402_confirm, "current_block",
                        lambda network=None, timeout=15.0: 5_000_000)
    billing_before = len(store.billing_log)
    events_before = len(store.events)
    atts_before = dict(store.externality_attestations)
    yield
    del store.billing_log[billing_before:]
    del store.events[events_before:]
    store.externality_attestations.clear()
    store.externality_attestations.update(atts_before)


class _PayerFacilitator(FakeFacilitator):
    """FakeFacilitator that reports a caller-chosen payer address, so a test
    can control the EVM key behind the reported payer."""

    def __init__(self, payer: str, **kw):
        super().__init__(**kw)
        self._payer = payer

    def verify(self, payload, requirements):
        self.verify_calls.append((payload, requirements))
        return SimpleNamespace(is_valid=True, invalid_reason=None,
                               payer=self._payer)

    def settle(self, payload, requirements):
        self.settle_calls.append((payload, requirements))
        net = self.network or requirements.network
        return SimpleNamespace(success=True, error_reason=None,
                               transaction="0x" + uuid.uuid4().hex * 2,
                               network=net, payer=self._payer)


def _cap():
    return "honesty-" + uuid.uuid4().hex[:8]


def _last_payment():
    return [b for b in store.billing_log
            if b.get("type") == "x402_payment"][-1]


def _proof_header(priv, did, resource):
    env = callerproof.create_proof(priv, did, method="GET",
                                   resource=resource, body=b"")
    return base64.b64encode(json.dumps(env).encode()).decode()


def _bound_did(address=PAYER, network=MAINNET):
    priv, pub = crypto.generate_keypair()
    did = crypto.did_from_public_key(pub)
    cred = walletbinding.issue_credential(store, did=did, address=address,
                                          network=network,
                                          challenge_nonce="t-" +
                                          uuid.uuid4().hex)
    return priv, did, cred


def _paid_get(client, cap, extra_headers=None):
    preq = payments.check_request(cap)
    headers = {"User-Agent": EXT_UA,
               "PAYMENT-SIGNATURE": sig_header(make_payload(preq))}
    headers.update(extra_headers or {})
    return client.get(f"/check?capability={cap}", headers=headers)


# ---------------------------------------------------------------------------
# THE decisive negative test: a Guild-controlled process creates a fresh DID
# and wallet, binds them through the REAL dual-signature flow, and pays.
# ---------------------------------------------------------------------------

def test_self_created_did_and_wallet_never_becomes_attested_external(
        monkeypatch):
    """An Agent Guild-controlled process (this test) mints a fresh did:key
    and a fresh EVM wallet, completes the real /wallet-binding challenge
    flow, and pays with a valid caller proof. The settlement MAY become
    cryptographically bound — it must NEVER enter independently attested
    external revenue, and no surface may call it verified external."""
    from app.main import app
    acct = Account.create()                    # a wallet WE control
    payer = acct.address.lower()
    monkeypatch.setattr(
        x402, "_facilitator",
        lambda: _PayerFacilitator(payer, network=MAINNET))
    cap = _cap()
    with TestClient(app) as client:
        priv, pub = crypto.generate_keypair()  # a DID WE control
        did = crypto.did_from_public_key(pub)
        ch = client.post("/wallet-binding/challenge",
                         json={"did": did}).json()
        binding = walletbinding.binding_payload(
            did=did, address=payer, network=MAINNET,
            nonce=ch["nonce"], expires_at=ch["expires_at"])
        did_sig = crypto.sign_jcs(binding, priv)
        evm_sig = Account.sign_message(
            encode_defunct(text=walletbinding.binding_message(binding)),
            acct.key).signature.hex()
        r = client.post("/wallet-binding/verify",
                        json={"binding": binding, "did_signature": did_sig,
                              "evm_signature": evm_sig})
        assert r.status_code == 200, r.text
        cred = r.json()["credential"]

        resource = f"/check?capability={cap}"
        preq = payments.check_request(cap)
        payload = make_payload(preq)
        payload.payload["authorization"]["from"] = payer
        r = client.get(resource, headers={
            "User-Agent": EXT_UA,
            "PAYMENT-SIGNATURE": sig_header(payload),
            callerproof.HTTP_HEADER: _proof_header(priv, did, resource)})
        assert r.status_code == 200, r.text

        rec = _last_payment()
        assert rec["payer_attribution"] == \
            "cryptographically_bound_machine_payer", (
                "identity + wallet control are proven — externality is NOT")
        assert rec["caller_did"] == did
        assert rec["wallet_binding_credential"] == cred["credential_id"]
        # ownership unproven ⇒ the legacy tri-state flag stays UNKNOWN
        assert rec["first_party_payer"] is None

        rev = client.get("/billing/revenue").json()["real_settlement"]
        att = rev["attribution"]
        assert set(att) == CLASSES
        assert att["independently_attested_external_machine"][
            "transactions"] == 0, (
            "no independent attestation mechanism has issued anything — "
            "this total must be honestly zero")
        assert rev["independently_attested_external_revenue_usd"] == 0.0
        assert att["cryptographically_bound_machine_payer"][
            "transactions"] >= 1
        assert rev["cryptographically_bound_machine_revenue_usd"] == \
            rev["attribution"]["cryptographically_bound_machine_payer"][
                "revenue_usd"]
        assert "verified_external_revenue_usd" not in rev, (
            "unknown ownership must never be labelled verified external")

        funnel = {s["stage"]: s["count"]
                  for s in client.get("/funnel").json()["stages"]}
        assert "verified_external_machine_settlement" not in funnel
        assert funnel["cryptographically_bound_machine_settlement"] >= 1
        assert funnel["independently_attested_external_settlement"] == 0


# ---------------------------------------------------------------------------
# class semantics
# ---------------------------------------------------------------------------

def test_missing_proof_stays_unverified_never_external():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        r = _paid_get(client, cap)
        assert r.status_code == 200
    rec = _last_payment()
    assert rec["payer_attribution"] == "unverified_payer"
    assert rec["first_party_payer"] is None
    assert rec["confirmed"] is True            # money still counts


def test_guild_operated_did_is_canary():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        priv, pub = crypto.generate_keypair()
        agent = store.register_agent(name="canary-honesty",
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
    assert rec["first_party_payer"] is True


def test_classifier_only_emits_the_conservative_classes():
    priv, did, cred = _bound_did()
    for kwargs in (
            dict(payer=PAYER, network=MAINNET, caller_did=did),
            dict(payer=PAYER, network=MAINNET, caller_did=""),
            dict(payer="", network=MAINNET, caller_did=did),
            dict(payer=PAYER, network=MAINNET, caller_did=did,
                 first_party_flag=True)):
        out = payments.classify_payer_attribution(store, **kwargs)
        assert out["class"] in CLASSES
        assert out["class"] != "verified_external_machine"


def test_legacy_verified_external_records_reinterpreted_conservatively():
    """Historical billing records written under the old class name are
    re-interpreted at READ time as cryptographically bound (their evidence
    only ever proved binding) — never as attested external."""
    from app.main import app
    store.billing_log.append({
        "key": "x402", "type": "x402_payment", "endpoint": "check",
        "network": MAINNET, "amount_atomic": "10000",
        "payer": PAYER, "transaction": "0x" + "ab" * 32,
        "status": "settled_confirmed", "mainnet": True, "confirmed": True,
        "payer_attribution": "verified_external_machine",
        "caller_did": "did:key:z6MkLegacy", "at": "2026-07-15T00:00:00Z"})
    with TestClient(app) as client:
        rev = client.get("/billing/revenue").json()["real_settlement"]
    att = rev["attribution"]
    assert att["independently_attested_external_machine"]["transactions"] \
        == 0
    assert att["cryptographically_bound_machine_payer"]["transactions"] >= 1


# ---------------------------------------------------------------------------
# the independent-attestation mechanism: real, but honestly zero by default
# ---------------------------------------------------------------------------

def _attestation(issuer_priv, issuer_did, subject_did, *,
                 issued_at=None, expires_at=None, claim=None):
    body = {
        "v": externality.PROTOCOL,
        "subject_did": subject_did,
        "issuer_did": issuer_did,
        "claim": claim or externality.CLAIM,
        "issued_at": issued_at or "2026-01-01T00:00:00+00:00",
        "expires_at": expires_at or "2099-01-01T00:00:00+00:00",
    }
    return {**body, "proof": crypto.sign_jcs(body, issuer_priv)}


def test_attestation_requires_allowlisted_issuer(monkeypatch):
    priv, did, cred = _bound_did()
    ipriv, ipub = crypto.generate_keypair()
    issuer_did = crypto.did_from_public_key(ipub)
    att = _attestation(ipriv, issuer_did, did)
    store.record_externality_attestation(att)
    # NOT allowlisted (the default: empty allowlist) → still only bound
    out = payments.classify_payer_attribution(
        store, payer=PAYER, network=MAINNET, caller_did=did)
    assert out["class"] == "cryptographically_bound_machine_payer"
    # allowlisted → independently attested external
    monkeypatch.setenv("GUILD_EXTERNALITY_ATTESTOR_DIDS", issuer_did)
    out = payments.classify_payer_attribution(
        store, payer=PAYER, network=MAINNET, caller_did=did)
    assert out["class"] == "independently_attested_external_machine"


def test_attestation_tamper_expiry_and_subject_all_fail(monkeypatch):
    priv, did, cred = _bound_did()
    ipriv, ipub = crypto.generate_keypair()
    issuer_did = crypto.did_from_public_key(ipub)
    monkeypatch.setenv("GUILD_EXTERNALITY_ATTESTOR_DIDS", issuer_did)

    # tampered signature
    att = _attestation(ipriv, issuer_did, did)
    att["claim"] = "TAMPERED"
    store.record_externality_attestation(att)
    out = payments.classify_payer_attribution(
        store, payer=PAYER, network=MAINNET, caller_did=did)
    assert out["class"] == "cryptographically_bound_machine_payer"

    # expired
    att = _attestation(ipriv, issuer_did, did,
                       expires_at="2026-01-02T00:00:00+00:00")
    store.record_externality_attestation(att)
    out = payments.classify_payer_attribution(
        store, payer=PAYER, network=MAINNET, caller_did=did)
    assert out["class"] == "cryptographically_bound_machine_payer"

    # different subject
    att = _attestation(ipriv, issuer_did, "did:key:z6MkSomeoneElse")
    store.record_externality_attestation(att)
    out = payments.classify_payer_attribution(
        store, payer=PAYER, network=MAINNET, caller_did=did)
    assert out["class"] == "cryptographically_bound_machine_payer"


def test_self_issued_attestation_never_counts(monkeypatch):
    """The subject vouching for itself is not independent — even if its own
    DID somehow lands on the allowlist."""
    priv, did, cred = _bound_did()
    monkeypatch.setenv("GUILD_EXTERNALITY_ATTESTOR_DIDS", did)
    att = _attestation(priv, did, did)
    store.record_externality_attestation(att)
    out = payments.classify_payer_attribution(
        store, payer=PAYER, network=MAINNET, caller_did=did)
    assert out["class"] == "cryptographically_bound_machine_payer"


def test_guild_issued_attestation_never_counts(monkeypatch):
    """The Guild attesting to a payer's externality is not independent."""
    priv, did, cred = _bound_did()
    gid = store.guild_identity()
    monkeypatch.setenv("GUILD_EXTERNALITY_ATTESTOR_DIDS", gid["did"])
    att = _attestation(gid["private_key"], gid["did"], did)
    store.record_externality_attestation(att)
    out = payments.classify_payer_attribution(
        store, payer=PAYER, network=MAINNET, caller_did=did)
    assert out["class"] == "cryptographically_bound_machine_payer"


def test_valid_allowlisted_attestation_reaches_revenue(monkeypatch):
    """End-to-end: with a REAL allowlisted independent attestation the class
    flows into receipts + revenue — proving zero-by-default is a fact about
    the world, not a hardcoded zero."""
    from app.main import app
    cap = _cap()
    priv, did, cred = _bound_did()
    ipriv, ipub = crypto.generate_keypair()
    issuer_did = crypto.did_from_public_key(ipub)
    monkeypatch.setenv("GUILD_EXTERNALITY_ATTESTOR_DIDS", issuer_did)
    store.record_externality_attestation(_attestation(ipriv, issuer_did, did))
    with TestClient(app) as client:
        resource = f"/check?capability={cap}"
        r = _paid_get(client, cap, {
            callerproof.HTTP_HEADER: _proof_header(priv, did, resource)})
        assert r.status_code == 200
        rec = _last_payment()
        assert rec["payer_attribution"] == \
            "independently_attested_external_machine"
        assert rec["first_party_payer"] is False
        rev = client.get("/billing/revenue").json()["real_settlement"]
        assert rev["independently_attested_external_revenue_usd"] > 0.0
