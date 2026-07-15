"""Signed offer/receipt extension + Agent Guild evidence attachment.

Every 402 carries a JWS-signed offer; every served payment returns a
JWS-signed receipt plus the Guild's namespaced evidence attachment. Signed by
the persistent Guild SERVICE-signing key under its did:web identity
(did:web:<service origin>#<key multibase>), whose DID document is published
at {origin}/.well-known/did.json — NEVER the treasury key.

Standards conformance is asserted by an INDEPENDENT official TypeScript
verifier (verifiers/x402_offer_receipt_verify.mjs → @x402/extensions, pinned
exact version) when Node is available. That harness performs REAL did:web
resolution against a live local DID-document server, proving the whole chain
origin → DID document → authorised key → signature, plus key-substitution
and hostile-origin failures. The Python self-checks alone never claim
compliance.
"""
import base64
import http.server
import json
import pathlib
import shutil
import socket
import subprocess
import threading

import pytest

from app import x402, x402_artifacts as artifacts
from app.crypto import (did_web_from_origin, generate_keypair,
                        public_key_multibase)
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


# --- signing identity: did:web bound to the service origin, never treasury ---

def test_signing_identity_is_did_web_service_key_not_treasury():
    gid = _identity()
    kid = artifacts.kid_for_identity(gid)
    # documented profile: did:web of the configured public origin
    assert kid.startswith("did:web:")
    assert kid.split("#")[0] == did_web_from_origin(x402.public_host())
    # fragment authorises the persistent Ed25519 SERVICE key
    assert kid.split("#")[1] == public_key_multibase(gid["public_key"])
    # the treasury is an EVM 0x address; it appears nowhere in the kid
    assert x402.MAINNET_TREASURY.lower() not in kid.lower()
    assert not kid.startswith("0x")


def test_kid_matches_origin_binding():
    gid = _identity()
    kid = artifacts.kid_for_identity(gid)
    assert artifacts.kid_matches_origin(kid, x402.public_host())
    assert not artifacts.kid_matches_origin(kid, "https://evil.example")
    hostile = "did:web:evil.example#" + kid.split("#")[1]
    assert not artifacts.kid_matches_origin(hostile, x402.public_host())


def test_did_json_route_serves_did_web_document():
    from app.main import app
    from fastapi.testclient import TestClient
    gid = _identity()
    with TestClient(app) as client:
        r = client.get("/.well-known/did.json")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/did+json")
        doc = r.json()
        assert doc["id"] == did_web_from_origin(x402.public_host())
        kid = artifacts.kid_for_identity(gid)
        methods = {m["id"]: m for m in doc["verificationMethod"]}
        assert kid in methods
        assert methods[kid]["publicKeyMultibase"] == \
            public_key_multibase(gid["public_key"])
        assert kid in doc["assertionMethod"]
        # the AGI-1 did:key identity of the SAME key is cross-linked
        assert gid["did"] in doc["alsoKnownAs"]
        # no EVM material, ever
        assert x402.MAINNET_TREASURY.lower() not in r.text.lower()


def test_did_document_publishes_the_offer_receipt_key_binding():
    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        doc = client.get("/.well-known/agent-guild-did.json").json()
        binding = doc["x402_offer_receipt"]
        assert binding["alg"] == "EdDSA"
        assert binding["kid"] == artifacts.kid_for_identity(_identity())
        assert binding["did_document"] == "/.well-known/did.json"
        assert "offer-receipt" in binding["extensions"]
        assert binding["authorized_origin"] == x402.public_host()


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


# --- INDEPENDENT official TypeScript verifier over REAL did:web resolution ---

class _DidDocServer:
    """A real local HTTP server publishing a DID document at
    /.well-known/did.json — what did:web resolution actually fetches.
    localhost origins resolve over http per the did:web dev convention the
    official resolver implements."""

    def __init__(self, doc: dict):
        self.doc = doc
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):                       # noqa: N802
                if self.path == "/.well-known/did.json":
                    body = json.dumps(outer.doc).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/did+json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)

            def log_message(self, *a):               # quiet
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.origin = f"http://localhost:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def did_servers(monkeypatch):
    """The GENUINE origin serving the Guild service key's DID document (the
    kid's origin — GUILD_PUBLIC_HOST is pointed at it so signatures bind to
    it), plus two attack servers: one substituting a different key under the
    genuine document shape, one a hostile origin with its own valid
    identity."""
    gid = _identity()
    genuine = _DidDocServer({})
    monkeypatch.setenv("GUILD_PUBLIC_HOST", genuine.origin)
    genuine.doc = artifacts.did_web_document(gid, origin=genuine.origin)

    # key substitution: same origin-shaped document, DIFFERENT authorised key
    sub_priv, sub_pub = generate_keypair()
    substituted = _DidDocServer({})
    substituted.doc = artifacts.did_web_document(
        {"did": gid["did"], "public_key": sub_pub, "private_key": sub_priv},
        origin=substituted.origin)

    # hostile origin: attacker's own internally-VALID did:web identity
    evil_priv, evil_pub = generate_keypair()
    evil = _DidDocServer({})
    evil_identity = {"did": "did:key:attacker", "public_key": evil_pub,
                     "private_key": evil_priv}
    evil.doc = artifacts.did_web_document(evil_identity, origin=evil.origin)

    try:
        yield genuine, substituted, evil, evil_identity
    finally:
        genuine.close()
        substituted.close()
        evil.close()


def _write_vector(tmp_path, genuine, substituted, evil, evil_identity):
    gid = _identity()
    other_priv, _ = generate_keypair()
    # a DIFFERENT signer whose kid still points at the GUILD origin (so the
    # verifier resolves the GENUINE key but the signature was made by a
    # foreign key → must fail)
    wrong_identity = {"did": gid["did"], "public_key": gid["public_key"],
                      "private_key": other_priv}

    resource = genuine.origin + "/check?capability=x"
    offer_payload = artifacts.offer_payload(
        resource_url=resource, scheme="exact", network="eip155:8453",
        asset=x402.USDC_BY_NETWORK["eip155:8453"], pay_to=PAY_TO,
        amount="10000")
    receipt_payload = artifacts.receipt_payload(
        network="eip155:8453", resource_url=resource,
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

    # key substitution: the GENUINE key signs, but the kid resolves to a
    # registry whose DID document authorises a DIFFERENT key — must fail.
    substituted_offer = {
        "format": "jws", "acceptIndex": 0,
        "signature": artifacts.jws_sign(
            offer_payload, gid["private_key"],
            substituted.doc["verificationMethod"][0]["id"]),
    }

    # hostile origin: attacker's internally-consistent identity signs an
    # offer for OUR resource — signature verifies against THEIR document.
    hostile_offer = {
        "format": "jws", "acceptIndex": 0,
        "signature": artifacts.jws_sign(
            offer_payload, evil_identity["private_key"],
            evil.doc["verificationMethod"][0]["id"]),
    }

    vector = {
        "valid_offer": valid_offer,
        "valid_receipt": valid_receipt,
        "tampered_offer": _tamper(valid_offer),
        "tampered_receipt": _tamper(valid_receipt),
        "wrong_key_offer": artifacts.signed_offer(wrong_identity,
                                                  offer_payload),
        "wrong_key_receipt": artifacts.signed_receipt(wrong_identity,
                                                      receipt_payload),
        "substituted_key_offer": substituted_offer,
        "hostile_origin_offer": hostile_offer,
        "expected": {
            "origin": genuine.origin,
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
def test_independent_official_typescript_verifier(tmp_path, did_servers):
    genuine, substituted, evil, evil_identity = did_servers
    verifier = REPO / "verifiers" / "x402_offer_receipt_verify.mjs"
    node_modules = REPO / "verifiers" / "node_modules" / "@x402" / "extensions"
    if not node_modules.exists():
        r = subprocess.run(
            ["npm", "install", "--no-fund", "--no-audit"],
            cwd=str(REPO / "verifiers"), capture_output=True, text=True,
            timeout=180)
        if r.returncode != 0:
            pytest.skip(f"npm install failed: {r.stderr[-300:]}")
    vec = _write_vector(tmp_path, genuine, substituted, evil, evil_identity)
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
