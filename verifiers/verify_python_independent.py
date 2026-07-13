"""INDEPENDENT Python verification of Agent Guild eddsa-jcs-2022 credentials.

Uses only third-party primitives — rfc8785 (Trail of Bits' RFC 8785/JCS
implementation), base58, and pyca/cryptography — with the W3C VC-DI-EdDSA
verification algorithm transcribed from the spec. No Agent Guild code imported.
"""
import json, sys, hashlib
import rfc8785, base58
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

def pub_from_did(did):
    mb = did[len("did:key:"):]
    raw = base58.b58decode(mb[1:])
    assert raw[:2] == bytes([0xED, 0x01]), "not an ed25519 did:key"
    return raw[2:]

def verify(vc):
    proof = dict(vc["proof"])
    assert proof["type"] == "DataIntegrityProof"
    assert proof["cryptosuite"] == "eddsa-jcs-2022"
    proof_value = proof.pop("proofValue")
    doc = {k: v for k, v in vc.items() if k != "proof"}
    vm = proof["verificationMethod"]; did = vm.split("#")[0]
    assert did == vc["issuer"], "verificationMethod is not the issuer's"
    hash_data = (hashlib.sha256(rfc8785.dumps(proof)).digest()
                 + hashlib.sha256(rfc8785.dumps(doc)).digest())
    sig = base58.b58decode(proof_value[1:])  # strip multibase 'z'
    try:
        Ed25519PublicKey.from_public_bytes(pub_from_did(did)).verify(sig, hash_data)
        return True
    except InvalidSignature:
        return False

vec = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "vector.json"))
ok = True
for name in ("credential", "passport"):
    vc = vec[name]
    v = verify(vc)
    t = json.loads(json.dumps(vc)); t["credentialSubject"]["rating"] = 0.1; t["credentialSubject"]["trust"] = 99
    tv = verify(t)
    print(f"{name} verified: {v}  tampered rejected: {not tv}")
    ok = ok and v and not tv
sys.exit(0 if ok else 1)
