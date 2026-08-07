"""Exact-wallet pre-payment resolution.

An ACP/x402 funding policy must never infer identity from a listing's claimed
wallet.  These tests pin the stronger contract: only the active credential for
the exact address + CAIP-2 network resolves, and the public response contains
no custodial secrets.
"""
from eth_account import Account
from fastapi.testclient import TestClient

from app import crypto, walletbinding
from app.store import Store

MAINNET = "eip155:8453"
TESTNET = "eip155:84532"


def _registered_bound(store: Store):
    private_key, public_key = crypto.generate_keypair()
    agent = store.register_agent(
        "wallet-bound-worker",
        ["fact-check"],
        {"endpoint": "https://worker.example/a2a"},
        public_key=public_key,
    )
    wallet = Account.create()
    credential = walletbinding.issue_credential(
        store,
        did=agent["did"],
        address=wallet.address,
        network=MAINNET,
        challenge_nonce="test-post-verification-state",
    )
    return private_key, agent, wallet, credential


def test_resolve_counterparty_binds_exact_wallet_network_and_agent():
    store = Store(path="")
    _private_key, agent, wallet, credential = _registered_bound(store)

    out = walletbinding.resolve_counterparty(
        store, wallet.address.upper().replace("0X", "0x"), MAINNET)

    assert out["status"] == "bound_registered"
    assert out["bound"] is True
    assert out["address"] == wallet.address.lower()
    assert out["binding"]["credential"]["credential_id"] == credential[
        "credential_id"]
    assert out["binding"]["status"]["status"] == "active"
    assert out["agent"] == {
        "id": agent["id"],
        "did": agent["did"],
        "name": "wallet-bound-worker",
        "capabilities": ["fact-check"],
        "endpoint": "https://worker.example/a2a",
        "reachability": agent.get("reachability"),
    }
    assert "api_key" not in out["agent"]
    assert "private_key" not in out["agent"]


def test_resolution_fails_closed_across_networks_and_after_revocation():
    store = Store(path="")
    _private_key, _agent, wallet, credential = _registered_bound(store)

    wrong_network = walletbinding.resolve_counterparty(
        store, wallet.address, TESTNET)
    assert wrong_network["status"] == "unbound"
    assert wrong_network["agent"] is None

    assert store.revoke_wallet_binding(credential["credential_id"])
    revoked = walletbinding.resolve_counterparty(
        store, wallet.address, MAINNET)
    assert revoked["status"] == "unbound"
    assert revoked["binding"] is None


def test_bound_did_without_guild_registration_is_not_given_reputation():
    store = Store(path="")
    _private_key, public_key = crypto.generate_keypair()
    did = crypto.did_from_public_key(public_key)
    wallet = Account.create()
    walletbinding.issue_credential(
        store,
        did=did,
        address=wallet.address,
        network=MAINNET,
        challenge_nonce="test-post-verification-state",
    )

    out = walletbinding.resolve_counterparty(store, wallet.address, MAINNET)
    assert out["status"] == "bound_unregistered"
    assert out["bound"] is True
    assert out["agent"] is None


def test_public_route_precedes_dynamic_status_route_and_links_paid_risk(
    monkeypatch,
):
    from app import main

    local = Store(path="")
    _private_key, agent, wallet, _credential = _registered_bound(local)
    monkeypatch.setattr(main, "store", local)

    with TestClient(main.app) as client:
        response = client.get(
            "/wallet-binding/resolve",
            params={"address": wallet.address, "network": MAINNET},
        )

    assert response.status_code == 200, response.text
    out = response.json()
    assert out["status"] == "bound_registered"
    assert out["agent"]["id"] == agent["id"]
    assert out["next"]["risk_score"].endswith(
        f"/agents/{agent['id']}/risk-score")
    assert "metered" in out["next"]["economics"]


def test_resolution_rejects_malformed_addresses_and_unknown_networks():
    store = Store(path="")
    for address, network in (
        ("0x1234", MAINNET),
        ("0x" + "g" * 40, MAINNET),
        ("0x" + "1" * 40, "eip155:1"),
    ):
        try:
            walletbinding.resolve_counterparty(store, address, network)
        except walletbinding.BindingError:
            pass
        else:  # pragma: no cover - assertion spelling keeps error readable
            raise AssertionError((address, network))
