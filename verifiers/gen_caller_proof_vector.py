"""Generate the bundled caller-proof + wallet-binding verification vector.

Produces verifiers/caller_proof_vector.json from the LIVE issuer/caller code
(app.callerproof, app.walletbinding, app.crypto). The independent Python and
Node verifiers then check these vectors WITHOUT importing Agent Guild code —
proving the agent-guild/caller-proof/v1 envelope and the wallet-binding
credential verify offline anywhere.

Usage: python verifiers/gen_caller_proof_vector.py [out.json]
"""
import json
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "live" / "guild"))

from app import callerproof, crypto, walletbinding   # noqa: E402


def _account_from_seed(seed: bytes):
    from eth_account import Account
    return Account.from_key(seed)


def main() -> int:
    out_path = sys.argv[1] if len(sys.argv) > 1 else str(
        REPO / "verifiers" / "caller_proof_vector.json")

    # a caller's self-controlled did:key
    priv, pub = crypto.generate_keypair()
    did = crypto.did_from_public_key(pub)

    now = 1_760_000_000
    proof = callerproof.create_proof(
        priv, did, method="GET",
        resource="/check?capability=translation", body=b"", now=now)

    # a deterministic EVM account (fixed seed → reproducible vector)
    acct = _account_from_seed(b"\x11" * 32)
    nonce = "wb_fixed_vector_nonce"
    binding = walletbinding.binding_payload(
        did=did, address=acct.address, network="eip155:8453",
        nonce=nonce, expires_at="2099-01-01T00:00:00+00:00")
    from eth_account.messages import encode_defunct
    from eth_account import Account
    did_sig = crypto.sign_jcs(binding, priv)
    evm_sig = Account.sign_message(
        encode_defunct(text=walletbinding.binding_message(binding)),
        acct.key).signature.hex()

    vector = {
        "note": ("Independent verification vectors for "
                 "agent-guild/caller-proof/v1 and the wallet-binding "
                 "credential. Verify OFFLINE with no Agent Guild code."),
        "generated_at": int(time.time()),
        "caller_proof": {
            "envelope": proof,
            "request": {"method": "GET",
                        "resource": "/check?capability=translation",
                        "body_utf8": ""},
            "public_key_hex": pub,
            "expected_did": did,
        },
        "wallet_binding": {
            "binding": binding,
            "did_signature": did_sig,
            "evm_signature": evm_sig,
            "did_public_key_hex": pub,
            "expected_evm_address": acct.address,
            "message": walletbinding.binding_message(binding),
        },
    }
    pathlib.Path(out_path).write_text(json.dumps(vector, indent=2) + "\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
