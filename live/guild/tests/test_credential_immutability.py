"""Issued wallet-binding credentials are IMMUTABLE.

Defect corrected: revocation/supersession used to mutate fields inside the
SIGNED credential document, so the stored credential's own signature failed
after any status change — an issued credential could not be re-verified
against the store. Now:

  * the signed credential document never changes after signing;
  * live status (active/revoked/superseded, timestamps, successor id) lives
    in a SEPARATE record keyed by credential_id;
  * offline cryptographic validity holds until credential expiry, whatever
    the live status;
  * settlement attribution accepts ACTIVE live status only;
  * legacy persisted records (embedded status) read conservatively;
  * a revoked credential can never be re-activated by replay;
  * GET /wallet-binding/status/{credential_id} returns the immutable
    credential + current status + as_of + issuer DID + a Guild signature
    over the status body.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import crypto, payments, walletbinding
from app.state import store

MAINNET = "eip155:8453"
PAYER = "0x" + "22" * 20


def _did():
    priv, pub = crypto.generate_keypair()
    return priv, crypto.did_from_public_key(pub)


def _cred(did, address=PAYER, network=MAINNET):
    return walletbinding.issue_credential(
        store, did=did, address=address, network=network,
        challenge_nonce="im-" + uuid.uuid4().hex)


def _stored_bytes(cred_id):
    return json.dumps(store.wallet_bindings[cred_id], sort_keys=True)


def test_revocation_never_mutates_the_signed_document():
    priv, did = _did()
    cred = _cred(did)
    cid = cred["credential_id"]
    before = _stored_bytes(cid)
    assert store.revoke_wallet_binding(cid) is True
    assert _stored_bytes(cid) == before, (
        "revocation mutated the signed credential document")
    # offline cryptographic validity SURVIVES revocation (until expiry) …
    assert walletbinding.credential_offline_valid(store, cred) is True
    assert walletbinding.credential_offline_valid(
        store, store.wallet_bindings[cid]) is True, (
        "the STORED credential's signature must still verify after "
        "revocation — this was the defect")
    # … while LIVE status and the combined check say no.
    assert walletbinding.credential_status_live(store, cred) is False
    assert walletbinding.verify_credential(store, cred) is False


def test_supersession_never_mutates_the_signed_document():
    addr = ("0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8])[:42].lower()
    _, did_a = _did()
    _, did_b = _did()
    cred_a = _cred(did_a, address=addr)
    before = _stored_bytes(cred_a["credential_id"])
    cred_b = _cred(did_b, address=addr)
    assert _stored_bytes(cred_a["credential_id"]) == before
    st = store.wallet_binding_status_get(cred_a["credential_id"])
    assert st["status"] == "superseded"
    assert st["superseded_by"] == cred_b["credential_id"]
    assert walletbinding.credential_offline_valid(store, cred_a) is True
    assert walletbinding.credential_status_live(store, cred_a) is False


def test_attribution_only_accepts_active_live_status():
    priv, did = _did()
    cred = _cred(did)
    out = payments.classify_payer_attribution(
        store, payer=PAYER, network=MAINNET, caller_did=did)
    assert out["class"] == "cryptographically_bound_machine_payer"
    store.revoke_wallet_binding(cred["credential_id"])
    out = payments.classify_payer_attribution(
        store, payer=PAYER, network=MAINNET, caller_did=did)
    assert out["class"] == "unverified_payer", (
        "a revoked binding must never attribute a settlement")


def test_revoked_can_never_become_active_again():
    priv, did = _did()
    cred = _cred(did)
    cid = cred["credential_id"]
    store.revoke_wallet_binding(cid)
    # replaying an 'active' transition must be refused, terminally
    assert store.wallet_binding_status_set(cid, "active") is False
    assert store.wallet_binding_status_get(cid)["status"] == "revoked"
    assert store.wallet_binding_status_set(cid, "superseded") is False
    assert store.wallet_binding_status_get(cid)["status"] == "revoked"


def test_legacy_embedded_status_reads_conservatively():
    """Records persisted before the split carried status INSIDE the signed
    document. Reads must honor them without rewriting anything."""
    gid = store.guild_identity()
    legacy = {
        "type": "AgentGuildWalletBinding",
        "protocol": walletbinding.PROTOCOL,
        "credential_id": "wbc_legacy" + uuid.uuid4().hex[:8],
        "did": "did:key:z6MkLegacySubject",
        "address": "0x" + "77" * 20,
        "network": MAINNET,
        "issued_at": "2026-07-01T00:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "issuer": gid["did"],
        "challenge_nonce": "legacy",
        "status": "revoked",
        "revoked_at": "2026-07-02T00:00:00+00:00",
    }
    store.wallet_bindings[legacy["credential_id"]] = legacy
    st = store.wallet_binding_status_get(legacy["credential_id"])
    assert st["status"] == "revoked"
    assert st.get("legacy_embedded_status") is True
    # terminal even for legacy: cannot be resurrected
    assert store.wallet_binding_status_set(
        legacy["credential_id"], "active") is False
    # a legacy ACTIVE record still reads active
    legacy_active = {**legacy,
                     "credential_id": "wbc_legacyact" + uuid.uuid4().hex[:6],
                     "status": "active"}
    legacy_active.pop("revoked_at")
    store.wallet_bindings[legacy_active["credential_id"]] = legacy_active
    assert store.wallet_binding_status_get(
        legacy_active["credential_id"])["status"] == "active"


def test_status_endpoint_returns_signed_live_status():
    from app.main import app
    priv, did = _did()
    cred = _cred(did)
    cid = cred["credential_id"]
    with TestClient(app) as client:
        r = client.get(f"/wallet-binding/status/{cid}")
        assert r.status_code == 200
        doc = r.json()
        # the immutable credential document, byte-for-byte as stored
        assert doc["credential"] == store.wallet_bindings[cid]
        st = doc["status"]
        assert st["status"] == "active"
        assert st["credential_id"] == cid
        assert st["as_of"]
        gid = store.guild_identity()
        assert st["issuer"] == gid["did"]
        body = {k: v for k, v in st.items() if k != "proof"}
        assert crypto.verify_jcs(body, st["proof"], gid["public_key"]), (
            "the status response must carry a verifiable Guild signature")
        # after revocation the endpoint reports revoked — credential intact
        store.revoke_wallet_binding(cid)
        r2 = client.get(f"/wallet-binding/status/{cid}")
        doc2 = r2.json()
        assert doc2["status"]["status"] == "revoked"
        assert doc2["credential"] == doc["credential"]
        # unknown credential → 404
        assert client.get("/wallet-binding/status/wbc_nope").status_code \
            == 404


def test_full_flow_credential_has_no_mutable_fields():
    from eth_account import Account
    from eth_account.messages import encode_defunct
    from app.main import app
    priv, did = _did()
    acct = Account.create()
    with TestClient(app) as client:
        ch = client.post("/wallet-binding/challenge",
                         json={"did": did}).json()
        binding = walletbinding.binding_payload(
            did=did, address=acct.address, network=MAINNET,
            nonce=ch["nonce"], expires_at=ch["expires_at"])
        did_sig = crypto.sign_jcs(binding, priv)
        evm_sig = Account.sign_message(
            encode_defunct(text=walletbinding.binding_message(binding)),
            acct.key).signature.hex()
        cred = client.post("/wallet-binding/verify", json={
            "binding": binding, "did_signature": did_sig,
            "evm_signature": evm_sig}).json()["credential"]
    assert "status" not in cred
    assert "revoked_at" not in cred and "superseded_by" not in cred
    assert store.wallet_binding_status_get(
        cred["credential_id"])["status"] == "active"
