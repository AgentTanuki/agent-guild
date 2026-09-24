"""The Guild's own A2A agent card is signed (A2A spec §8.4) — so the Guild
meets the `agent_card_signed` bar /preflight applies to everyone else — and
the signature is independently verifiable from public material only."""
import base64
import json
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402

from app import a2a_card_signing as cs  # noqa: E402
from app import crypto  # noqa: E402
from app import preflight  # noqa: E402
from app.main import app  # noqa: E402
from app.state import store  # noqa: E402

client = TestClient(app)


def _b64url_json(s: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))


def test_card_carries_one_jws_signature_with_spec_header():
    for path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
        card = client.get(path).json()
        sigs = card["signatures"]
        assert isinstance(sigs, list) and len(sigs) == 1
        assert set(sigs[0]) == {"protected", "signature"}
        hdr = _b64url_json(sigs[0]["protected"])
        assert hdr["alg"] == "EdDSA" and hdr["typ"] == "JOSE"
        assert hdr["kid"].startswith("did:web:")
        assert "jku" not in hdr  # optional; kept out for the 5 KiB card budget


def test_signature_verifies_against_published_public_material():
    """Spec §8.4.3 from the outside: resolve kid via did.json AND via
    jwks.json (kid-matched), both must name the same Ed25519 key, and the JWS over the
    JCS-canonical card (minus `signatures`) must verify with it."""
    card = client.get("/.well-known/agent-card.json").json()
    hdr = _b64url_json(card["signatures"][0]["protected"])
    did_doc = client.get("/.well-known/did.json").json()
    vm = [m for m in did_doc["verificationMethod"] if m["id"] == hdr["kid"]]
    assert vm, "kid must be a verification method in the did:web document"
    mb = vm[0]["publicKeyMultibase"]
    pub_hex = crypto.public_key_from_did("did:key:" + mb)
    jwks = client.get("/.well-known/jwks.json").json()
    jwk = [k for k in jwks["keys"] if k["kid"] == hdr["kid"]][0]
    assert jwk["kty"] == "OKP" and jwk["crv"] == "Ed25519"
    x = base64.urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4)).hex()
    assert x == pub_hex, "jwks and did.json must publish the same key"
    assert cs.verify_agent_card(card, pub_hex) == hdr


def test_tampered_card_fails_verification():
    card = client.get("/.well-known/agent-card.json").json()
    pub_hex = store.guild_identity()["public_key"]
    assert cs.verify_agent_card(card, pub_hex) is not None
    tampered = dict(card)
    tampered["name"] = "Agent Guild (impostor)"
    assert cs.verify_agent_card(tampered, pub_hex) is None
    other = crypto.generate_keypair()[1]
    assert cs.verify_agent_card(card, other) is None


def test_canonical_payload_is_rfc8785_and_excludes_signatures():
    card = {"b": 1, "a": {"y": [1, 2], "x": "é"}, "signatures": [{"x": "y"}]}
    assert cs.canonical_payload(card) == b'{"a":{"x":"\xc3\xa9","y":[1,2]},"b":1}'


def test_preflight_rates_our_own_card_as_signed():
    """The reason for the change: /preflight's presence check on our card."""
    card = client.get("/.well-known/agent-card.json").json()
    signed, why = preflight._card_is_signed(card)
    assert signed and "signatures" in why
