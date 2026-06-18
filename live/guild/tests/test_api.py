"""End-to-end API tests using FastAPI's TestClient (no network, no keys)."""
import os
os.environ["GUILD_DATA"] = ""  # in-memory only; no persistence during tests

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app, store  # noqa: E402
from app import crypto, vc  # noqa: E402

client = TestClient(app)


def _register(name, caps, seed=False, metadata=None):
    r = client.post("/agents/register", json={
        "name": name, "capabilities": caps, "seed": seed, "metadata": metadata or {},
    })
    assert r.status_code == 200, r.text
    return r.json()


def test_register_generates_did_and_key():
    a = _register("Alice", ["fact-check"])
    assert a["did"].startswith("did:key:z")
    assert a["custodial"] is True
    assert a["api_key"].startswith("sk_")
    # public key recoverable from the DID
    assert crypto.public_key_from_did(a["did"]) == a["public_key"]


def test_custodial_attestation_is_signed_and_verifies():
    issuer = _register("Reviewer", ["research"])
    subject = _register("Worker", ["fact-check"])
    r = client.post(
        "/attestations",
        headers={"X-API-Key": issuer["api_key"]},
        json={"issuer_id": issuer["id"], "subject_id": subject["id"],
              "capability": "fact-check", "rating": 0.9, "comment": "solid"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True
    # Independently verify the returned credential.
    assert vc.verify_credential(body["credential"]) is True
    # Tampering breaks verification.
    cred = dict(body["credential"])
    cred["credentialSubject"] = {**cred["credentialSubject"], "rating": 0.1}
    assert vc.verify_credential(cred) is False


def test_attestation_auth_required():
    issuer = _register("R2", ["research"])
    subject = _register("W2", ["fact-check"])
    r = client.post(
        "/attestations",
        headers={"X-API-Key": "sk_wrong"},
        json={"issuer_id": issuer["id"], "subject_id": subject["id"],
              "capability": "fact-check", "rating": 0.8},
    )
    assert r.status_code == 401


def test_no_self_attestation():
    a = _register("Solo", ["fact-check"])
    r = client.post(
        "/attestations",
        headers={"X-API-Key": a["api_key"]},
        json={"issuer_id": a["id"], "subject_id": a["id"],
              "capability": "fact-check", "rating": 1.0},
    )
    assert r.status_code == 400


def test_self_sovereign_signed_credential():
    # Agent holds its own key, registers its public key, signs its own VC.
    priv, pub = crypto.generate_keypair()
    did = crypto.did_from_public_key(pub)
    issuer = _register("SelfSovereign", ["research"], metadata={})
    # Re-register issuer as self-sovereign with our key:
    r = client.post("/agents/register", json={
        "name": "SS", "capabilities": ["research"], "public_key": pub,
    })
    issuer = r.json()
    assert issuer["custodial"] is False
    subject = _register("Target", ["fact-check"])
    cred = vc.issue_credential(
        cred_id="urn:att:selftest", types=["WorkAttestation"],
        issuer_did=did, issuer_private_hex=priv, subject_did=subject["did"],
        capability="fact-check", rating=0.77,
    )
    r = client.post("/attestations", json={"subject_id": subject["id"],
                                           "capability": "fact-check", "rating": 0.77,
                                           "credential": cred})
    assert r.status_code == 200, r.text
    assert r.json()["verified"] is True


def test_reputation_and_search_ranking():
    # Build a small graph: a seed reviewer attests two workers differently.
    seed = _register("SeedReviewer", ["research"], seed=True)
    good = _register("GoodChecker", ["fact-check"], metadata={"price_per_call": 0.02})
    weak = _register("WeakChecker", ["fact-check"], metadata={"price_per_call": 0.005})
    for _ in range(3):
        client.post("/attestations", headers={"X-API-Key": seed["api_key"]},
                    json={"issuer_id": seed["id"], "subject_id": good["id"],
                          "capability": "fact-check", "rating": 0.95})
        client.post("/attestations", headers={"X-API-Key": seed["api_key"]},
                    json={"issuer_id": seed["id"], "subject_id": weak["id"],
                          "capability": "fact-check", "rating": 0.3})
    rg = client.get(f"/agents/{good['id']}/reputation").json()
    rw = client.get(f"/agents/{weak['id']}/reputation").json()
    assert rg["trust"] > rw["trust"]

    s = client.get("/search", params={"capability": "fact-check"}).json()
    ids = [r["id"] for r in s["results"]]
    assert ids.index(good["id"]) < ids.index(weak["id"])  # good ranks above weak
