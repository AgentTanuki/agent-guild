"""DID ↔ payment-wallet binding (machine-attribution pass).

A no-transaction, no-gas dual-signature challenge: the agent's did:key signs
a canonical binding (EVM address, network, Guild audience, nonce, expiry)
and the EVM address signs the same binding with a standard recoverable
EIP-191 signature. Both verify ⇒ the Guild issues a signed, expiring
wallet-binding credential. Rotation and revocation are machine-executable
and append-only. A self-declared wallet address is never proof.
"""
import time
import uuid

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from app import crypto, walletbinding
from app.state import store

MAINNET = "eip155:8453"


def _agent_keys():
    priv, pub = crypto.generate_keypair()
    return priv, pub, crypto.did_from_public_key(pub)


def _bind(client, did, priv, acct, network=MAINNET):
    ch = client.post("/wallet-binding/challenge",
                     json={"did": did}).json()
    binding = walletbinding.binding_payload(
        did=did, address=acct.address, network=network,
        nonce=ch["nonce"], expires_at=ch["expires_at"])
    did_sig = crypto.sign_jcs(binding, priv)
    msg = encode_defunct(text=walletbinding.binding_message(binding))
    evm_sig = Account.sign_message(msg, acct.key).signature.hex()
    r = client.post("/wallet-binding/verify",
                    json={"binding": binding, "did_signature": did_sig,
                          "evm_signature": evm_sig})
    return r


def test_full_binding_flow_issues_a_guild_signed_expiring_credential():
    from app.main import app
    priv, pub, did = _agent_keys()
    acct = Account.create()
    with TestClient(app) as client:
        r = _bind(client, did, priv, acct)
        assert r.status_code == 200, r.text
        cred = r.json()["credential"]
        assert cred["did"] == did
        assert cred["address"].lower() == acct.address.lower()
        assert cred["network"] == MAINNET
        assert cred["expires_at"] > cred["issued_at"]
        assert cred["status"] == "active"
        # Guild-signed: verifiable offline against the Guild's public key
        gid = store.guild_identity()
        body = {k: v for k, v in cred.items() if k != "proof"}
        assert crypto.verify_jcs(body, cred["proof"], gid["public_key"])
        # active binding is resolvable by payer address
        active = store.active_wallet_binding(acct.address)
        assert active and active["credential_id"] == cred["credential_id"]


def test_missing_or_wrong_signatures_fail_closed():
    from app.main import app
    priv, pub, did = _agent_keys()
    other_priv, _, _ = _agent_keys()
    acct, other_acct = Account.create(), Account.create()
    with TestClient(app) as client:
        ch = client.post("/wallet-binding/challenge",
                         json={"did": did}).json()
        binding = walletbinding.binding_payload(
            did=did, address=acct.address, network=MAINNET,
            nonce=ch["nonce"], expires_at=ch["expires_at"])
        msg = encode_defunct(text=walletbinding.binding_message(binding))
        good_did_sig = crypto.sign_jcs(binding, priv)
        good_evm_sig = Account.sign_message(msg, acct.key).signature.hex()
        # wrong DID key
        r = client.post("/wallet-binding/verify", json={
            "binding": binding,
            "did_signature": crypto.sign_jcs(binding, other_priv),
            "evm_signature": good_evm_sig})
        assert r.status_code == 422 and "did" in r.text.lower()
        # wrong wallet key (recovers a different address)
        r = client.post("/wallet-binding/verify", json={
            "binding": binding, "did_signature": good_did_sig,
            "evm_signature": Account.sign_message(
                msg, other_acct.key).signature.hex()})
        assert r.status_code == 422
        # tampered binding after signing
        evil = dict(binding, address=other_acct.address)
        r = client.post("/wallet-binding/verify", json={
            "binding": evil, "did_signature": good_did_sig,
            "evm_signature": good_evm_sig})
        assert r.status_code == 422


def test_replayed_or_expired_challenge_nonce_fails():
    from app.main import app
    priv, pub, did = _agent_keys()
    acct = Account.create()
    with TestClient(app) as client:
        r = _bind(client, did, priv, acct)
        assert r.status_code == 200
        nonce = r.json()["credential"]["challenge_nonce"]
        binding = walletbinding.binding_payload(
            did=did, address=acct.address, network=MAINNET,
            nonce=nonce, expires_at=walletbinding._iso_in(3600))
        did_sig = crypto.sign_jcs(binding, priv)
        msg = encode_defunct(text=walletbinding.binding_message(binding))
        evm_sig = Account.sign_message(msg, acct.key).signature.hex()
        r2 = client.post("/wallet-binding/verify",
                         json={"binding": binding, "did_signature": did_sig,
                               "evm_signature": evm_sig})
        assert r2.status_code == 422 and "nonce" in r2.text.lower()


def test_self_declared_address_is_never_proof():
    """No challenge, no signatures — just claiming an address must never
    create a binding."""
    from app.main import app
    _, _, did = _agent_keys()
    acct = Account.create()
    with TestClient(app) as client:
        r = client.post("/wallet-binding/verify", json={
            "binding": {"did": did, "address": acct.address,
                        "network": MAINNET},
            "did_signature": "", "evm_signature": ""})
        assert r.status_code == 422
    assert store.active_wallet_binding(acct.address) is None


def test_rotation_and_revocation_are_machine_executable_and_audited():
    from app.main import app
    priv, pub, did = _agent_keys()
    acct1, acct2 = Account.create(), Account.create()
    with TestClient(app) as client:
        c1 = _bind(client, did, priv, acct1).json()["credential"]
        # ROTATE: bind a new wallet — plain machine call, no human
        c2 = _bind(client, did, priv, acct2).json()["credential"]
        assert store.active_wallet_binding(acct2.address)
        # REVOKE the first credential, signed by the DID
        action = {"action": "revoke",
                  "credential_id": c1["credential_id"], "did": did}
        r = client.post("/wallet-binding/revoke", json={
            "request": action,
            "did_signature": crypto.sign_jcs(action, priv)})
        assert r.status_code == 200
        assert store.active_wallet_binding(acct1.address) is None
        # append-only audit trail
        audit = [e for e in store.events
                 if e.get("type") == "wallet_binding_revoked"
                 and e.get("credential_id") == c1["credential_id"]]
        assert audit, "revocation must be auditable"
        # revocation signed by a DIFFERENT key fails
        other_priv, _, _ = _agent_keys()
        action2 = {"action": "revoke",
                   "credential_id": c2["credential_id"], "did": did}
        r = client.post("/wallet-binding/revoke", json={
            "request": action2,
            "did_signature": crypto.sign_jcs(action2, other_priv)})
        assert r.status_code == 422
        assert store.active_wallet_binding(acct2.address)


def test_expired_credential_is_not_active(monkeypatch):
    from app.main import app
    priv, pub, did = _agent_keys()
    acct = Account.create()
    with TestClient(app) as client:
        cred = _bind(client, did, priv, acct).json()["credential"]
    rec = store.wallet_bindings[cred["credential_id"]]
    rec["expires_at"] = "2020-01-01T00:00:00+00:00"
    assert store.active_wallet_binding(acct.address) is None
