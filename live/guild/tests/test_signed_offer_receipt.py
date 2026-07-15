"""Signed offer/receipt extension + Agent Guild evidence attachment.

Every 402 carries a JWS-signed offer; every served payment returns a
JWS-signed receipt plus the Guild's namespaced evidence attachment. Signed by
the persistent Guild service identity (did:key Ed25519), NEVER the treasury
key. Standards conformance is asserted by an INDEPENDENT official TypeScript
verifier (verifiers/x402_offer_receipt_verify.mjs → @x402/extensions) when
Node is available; the Python self-checks alone never claim compliance.
"""
import base64
import json
import os
import pathlib
import shutil
import subprocess
import time

import pytest

from app import x402, x402_artifacts as artifacts
from app.crypto import generate_keypair, did_from_public_key
from app.state import store

PAY_TO = "0x" + "11" * 20
PAYER = "0x" + "22" * 20
REPO = pathlib.Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    yield


def _identity():
    return store.guild_identity()


# --- signing identity is the SERVICE identity, never the treasury ------------

def test_signing_identity_is_guild_did_not_treasury():
    gid = _identity()
    kid = artifacts.kid_for_identity(gid)
    assert kid.startswith("did:key:z")
    # the treasury is an EVM 0x address; the signing kid is a did:key — they
    # are categorically different key material
    assert x402.MAINNET_TREASURY.lower() not in kid.lower()
    assert not kid.startswith("0x")


def test_did_document_publishes_the_offer_receipt_key_binding(monkeypatch):
    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        doc = client.get("/.well-known/agent-guild-did.json").json()
        binding = doc["x402_offer_receipt"]
        assert binding["alg"] == "EdDSA"
        assert binding["kid"] == artifacts.kid_for_identity(_identity())
        assert "offer-receipt" in binding["extensions"]


# --- JWS round-trips + tampering (Python side) -------------------------------

def test_offer_jws_verifies_and_tamper_fails():
    gid = _identity()
    payload = artifacts.offer_payload(
        resource_url="https://guild.example/check?capability=x",
        scheme="exact", network="eip155:84532",
        asset=x402.asset(), pay_to=PAY_TO, amount="10000")
    offer = artifacts.signed_offer(gid, payload)
    got = artifacts.jws_verify(offer["signature"], gid["public_key"])
    assert got == payload
    # flip one payload byte → verification fails
    parts = offer["signature"].split(".")
    bad = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    bad["amount"] = "1"
    parts[1] = base64.urlsafe_b64encode(
        json.dumps(bad, separators=(",", ":")).encode()).rstrip(b"=").decode()
    assert artifacts.jws_verify(".".join(parts), gid["public_key"]) is None


def test_receipt_jws_binds_resource_payer_tx():
    gid = _identity()
    payload = artifacts.receipt_payload(
        network="eip155:84532",
        resource_url="https://guild.example/check?capability=x",
        payer=PAYER, transaction="0x" + "cd" * 32)
    receipt = artifacts.signed_receipt(gid, payload)
    got = artifacts.jws_verify(receipt["signature"], gid["public_key"])
    assert got["payer"] == PAYER
    assert got["transaction"] == "0x" + "cd" * 32
    # a different key never verifies
    _, other_pub = generate_keypair()
    assert artifacts.jws_verify(receipt["signature"], other_pub) is None


# --- INDEPENDENT official TypeScript verifier --------------------------------

def _write_vector(tmp_path) -> pathlib.Path:
    gid = _identity()
    _, other_pub = generate_keypair()
    other_priv, other_pub2 = generate_keypair()
    # a DIFFERENT identity whose kid still points at the GUILD did (so the
    # verifier resolves the Guild key but the signature was made by a foreign
    # key → must fail)
    wrong_identity = {"did": gid["did"], "public_key": gid["public_key"],
                      "private_key": other_priv}

    offer_payload = artifacts.offer_payload(
        resource_url="https://guild.example/check?capability=x",
        scheme="exact", network="eip155:8453",
        asset=x402.USDC_BY_NETWORK["eip155:8453"], pay_to=PAY_TO,
        amount="10000")
    receipt_payload = artifacts.receipt_payload(
        network="eip155:8453",
        resource_url="https://guild.example/check?capability=x",
        payer=PAYER, transaction="0x" + "ab" * 32)

    valid_offer = artifacts.signed_offer(gid, offer_payload)
    valid_receipt = artifacts.signed_receipt(gid, receipt_payload)

    def _tamper(signed):
        parts = signed["signature"].split(".")
        body = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
        body["amount" if "amount" in body else "payer"] = "0x" + "99" * 20
        parts[1] = base64.urlsafe_b64encode(
            json.dumps(body).encode()).rstrip(b"=").decode()
        return {**signed, "signature": ".".join(parts)}

    vector = {
        "valid_offer": valid_offer,
        "valid_receipt": valid_receipt,
        "tampered_offer": _tamper(valid_offer),
        "tampered_receipt": _tamper(valid_receipt),
        "wrong_key_offer": artifacts.signed_offer(wrong_identity, offer_payload),
        "wrong_key_receipt": artifacts.signed_receipt(wrong_identity,
                                                      receipt_payload),
        "expected": {
            "offer_resource_url": offer_payload["resourceUrl"],
            "receipt_resource_url": receipt_payload["resourceUrl"],
            "amount": "10000", "pay_to": PAY_TO, "payer": PAYER,
            "transaction": "0x" + "ab" * 32,
        },
    }
    out = tmp_path / "offer_receipt_vector.json"
    out.write_text(json.dumps(vector))
    return out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_independent_official_typescript_verifier(tmp_path):
    verifier = REPO / "verifiers" / "x402_offer_receipt_verify.mjs"
    node_modules = REPO / "verifiers" / "node_modules" / "@x402" / "extensions"
    if not node_modules.exists():
        r = subprocess.run(
            ["npm", "install", "--no-fund", "--no-audit", "@x402/extensions"],
            cwd=str(REPO / "verifiers"), capture_output=True, text=True,
            timeout=180)
        if r.returncode != 0:
            pytest.skip(f"npm install failed: {r.stderr[-300:]}")
    vec = _write_vector(tmp_path)
    r = subprocess.run(["node", str(verifier), str(vec)],
                       capture_output=True, text=True, timeout=120)
    print(r.stdout)
    assert r.returncode == 0, r.stdout + r.stderr


# --- evidence attachment is a SIBLING extension, never alters standard fields -

def test_evidence_attachment_is_namespaced_and_self_signed():
    gid = _identity()
    cp = {"seq": 3, "hash": "abc", "url": "/ledger/checkpoints"}
    ext = artifacts.evidence_extension(
        gid, resource_url="https://guild.example/check?capability=x",
        request_hash="rh", response_sha256="sh", transaction="0x" + "cd" * 32,
        payer=PAYER, payment_identifier_sha256="pidsha", checkpoint=cp)
    info = ext["info"]
    assert info["responseSha256"] == "sh"
    assert info["agi1Checkpoint"] == cp
    # the JWS is verifiable and independent of the offer-receipt fields
    verified = artifacts.jws_verify(info["jws"], gid["public_key"])
    assert verified["responseSha256"] == "sh"
    assert verified["requestHash"] == "rh"
