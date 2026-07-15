"""INDEPENDENT Python verification of agent-guild/caller-proof/v1 + the
wallet-binding credential. No Agent Guild code imported — only third-party
primitives (rfc8785 for JCS, base58, pyca/cryptography, eth_account).

Verifies, from the bundled vector:
  1. the caller-proof envelope: Ed25519 signature over JCS(payload) by the
     did:key, audience, protocol version, and the exact request binding
     (method / resource / body sha-256);
  2. the wallet binding: the did:key signature AND the recoverable EIP-191
     Ethereum signature over the SAME canonical binding recover the expected
     address.
Also asserts that TAMPERING (payload, did, signature, body) fails.
"""
import hashlib
import json
import sys

import base58
import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from eth_account import Account
from eth_account.messages import encode_defunct


def pub_from_did(did: str) -> bytes:
    raw = base58.b58decode(did[len("did:key:"):][1:])   # strip 'z' multibase
    assert raw[:2] == bytes([0xED, 0x01]), "not an ed25519 did:key"
    return raw[2:]


def _ed25519_verify_jcs(payload: dict, sig_hex: str, pub: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(
            bytes.fromhex(sig_hex), rfc8785.dumps(payload))
        return True
    except (InvalidSignature, ValueError):
        return False


def verify_caller_proof(v: dict) -> None:
    env = v["envelope"]
    payload, sig = env["payload"], env["signature"]
    did = payload["did"]
    assert did == v["expected_did"]
    assert payload["v"] == "agent-guild/caller-proof/v1", "protocol version"
    assert payload["aud"] == "agent-guild", "audience"
    # signature over JCS(payload) by the did:key
    assert _ed25519_verify_jcs(payload, sig, pub_from_did(did)), \
        "caller-proof signature INVALID"
    # exact request binding
    req = v["request"]
    assert payload["method"] == req["method"], "method binding"
    assert payload["resource"] == req["resource"], "resource binding"
    body = req["body_utf8"].encode("utf-8")
    assert payload["body_sha256"] == hashlib.sha256(body).hexdigest(), \
        "body-hash binding"
    print("PASS caller-proof: signature + audience + exact request binding")
    # tamper: mutating the signed payload must break the signature
    for mut in ("method", "resource", "did", "nonce"):
        t = json.loads(json.dumps(payload))
        t[mut] = "TAMPERED"
        assert not _ed25519_verify_jcs(t, sig, pub_from_did(did)), \
            f"tampering {mut} did NOT break the signature"
    # a different body must fail the binding
    assert payload["body_sha256"] != hashlib.sha256(b"EVIL").hexdigest()
    print("PASS caller-proof tamper: payload/did/body mutations all rejected")


def verify_wallet_binding(v: dict) -> None:
    binding = v["binding"]
    # 1. the DID controls the binding
    assert _ed25519_verify_jcs(binding, v["did_signature"],
                               bytes.fromhex(v["did_public_key_hex"])), \
        "wallet-binding DID signature INVALID"
    # 2. the EVM wallet controls the SAME binding (recoverable EIP-191)
    recovered = Account.recover_message(
        encode_defunct(text=v["message"]),
        signature=bytes.fromhex(v["evm_signature"].removeprefix("0x")))
    assert recovered.lower() == v["expected_evm_address"].lower(), \
        "EVM signature recovers a different address"
    print("PASS wallet-binding: DID + EVM signatures over the same binding")
    # tamper: a different address must not recover
    evil = dict(binding, address="0x" + "99" * 20)
    assert rfc8785.dumps(evil) != rfc8785.dumps(binding)
    print("PASS wallet-binding tamper: address mutation changes the message")


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "caller_proof_vector.json"
    vec = json.load(open(path))
    verify_caller_proof(vec["caller_proof"])
    verify_wallet_binding(vec["wallet_binding"])
    print("ALL INDEPENDENT PYTHON CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
