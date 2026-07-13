"""Generate a fresh credential + passport test vector from the Guild issuer code.
Run from live/guild:  python ../../verifiers/gen_vector.py ../../verifiers/vector.json"""
import json, sys
sys.path.insert(0, ".")
from app.crypto import generate_keypair, did_from_public_key
from app.vc import issue_credential, issue_passport, verify_credential

priv, pub = generate_keypair()
did = did_from_public_key(pub)
cred = issue_credential(cred_id="urn:test:cred1", types=["AgentGuildAttestation"],
                        issuer_did=did, issuer_private_hex=priv,
                        subject_did="did:key:z6MkfExampleSubject", capability="summarize",
                        rating=0.95, task_id="task_abc", comment="test vector")
pp = issue_passport(cred_id="urn:test:pass1", issuer_did=did, issuer_private_hex=priv,
                    subject_did="did:key:z6MkfExampleSubject",
                    subject_claims={"trust": 44.2, "rank": 1, "verified_task_count": 3})
assert verify_credential(cred) and verify_credential(pp), "self-verification failed"
out = sys.argv[1] if len(sys.argv) > 1 else "vector.json"
json.dump({"credential": cred, "passport": pp, "issuer_did": did}, open(out, "w"), indent=1)
print(f"wrote {out}")
