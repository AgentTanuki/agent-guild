"""Proof-suite truth: new credentials are conforming W3C Data Integrity
(eddsa-jcs-2022); historical AGI-1 legacy credentials still verify; the legacy
label is never issued again. See docs/PROOF_SUITES.md."""
import copy

from app.crypto import (generate_keypair, did_from_public_key, sign_jcs,
                        did_key_verification_method)
from app.vc import (issue_credential, issue_passport, verify_credential,
                    VC_CONTEXT, DATA_INTEGRITY_CRYPTOSUITE)


def _issuer():
    priv, pub = generate_keypair()
    return priv, did_from_public_key(pub)


def _cred(priv, did, **kw):
    return issue_credential(
        cred_id="urn:t:1", types=["AgentGuildAttestation"], issuer_did=did,
        issuer_private_hex=priv, subject_did="did:key:zSubj", capability="x",
        rating=kw.get("rating", 0.9))


def test_new_credentials_carry_conforming_data_integrity_proof():
    priv, did = _issuer()
    cred = _cred(priv, did)
    p = cred["proof"]
    assert p["type"] == "DataIntegrityProof"
    assert p["cryptosuite"] == DATA_INTEGRITY_CRYPTOSUITE
    assert p["proofValue"].startswith("z")              # base58btc multibase
    assert p["verificationMethod"] == did_key_verification_method(did)
    assert p["verificationMethod"].split("#")[0] == did
    assert "Ed25519Signature2020" not in str(p)
    assert verify_credential(cred)


def test_new_passports_verify_and_tampering_breaks_them():
    priv, did = _issuer()
    pp = issue_passport(cred_id="urn:t:2", issuer_did=did, issuer_private_hex=priv,
                        subject_did="did:key:zSubj", subject_claims={"trust": 40.0})
    assert verify_credential(pp)
    bad = copy.deepcopy(pp)
    bad["credentialSubject"]["trust"] = 99.9
    assert not verify_credential(bad)


def test_proof_signed_by_another_key_does_not_verify_as_issuer():
    priv, did = _issuer()
    other_priv, other_did = _issuer()
    cred = _cred(other_priv, other_did)
    forged = copy.deepcopy(cred)
    forged["issuer"] = did                     # claim someone else issued it
    assert not verify_credential(forged)


def test_legacy_agi1_credentials_still_verify():
    """Simulate a pre-2026-07-13 credential exactly as the old issuer built it."""
    priv, did = _issuer()
    created = "2026-07-01T00:00:00+00:00"
    unsigned = {
        "@context": VC_CONTEXT,
        "id": "urn:legacy:1",
        "type": ["VerifiableCredential", "AgentGuildAttestation"],
        "issuer": did,
        "validFrom": created,
        "credentialSubject": {"id": "did:key:zSubj", "capability": "x",
                              "rating": 0.8, "taskId": "t", "comment": ""},
        "proof": {
            "type": "Ed25519Signature2020",
            "created": created,
            "verificationMethod": f"{did}#{did.split(':')[-1]}",
            "proofPurpose": "assertionMethod",
        },
    }
    proof_value = sign_jcs(unsigned, priv)
    legacy = dict(unsigned)
    legacy["proof"] = {**unsigned["proof"], "proofValue": proof_value}
    assert verify_credential(legacy)
    bad = copy.deepcopy(legacy)
    bad["credentialSubject"]["rating"] = 1.0
    assert not verify_credential(bad)


def test_unknown_proof_types_are_rejected():
    priv, did = _issuer()
    cred = _cred(priv, did)
    cred["proof"]["type"] = "SomethingElse"
    assert not verify_credential(cred)
