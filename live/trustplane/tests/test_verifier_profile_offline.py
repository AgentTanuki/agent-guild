"""Offline regressions for the verifier profile and the order of checks.

Reproduced on the pristine public verifier (b341a475, Root baseline
PYTHON-VERIFIER-ROOT-BASELINE.json): (1) ``within_validity`` accepted a
credential whose start lies one hour in the future, because only
``now <= validUntil`` was checked; (2) a synthetically signed credential
whose document ``issuer`` differs from the ``proof.verificationMethod``
controller verified as true. Both are closed here, and it is additionally
proven that a document rejected for those reasons never establishes a TOFU
issuer pin (client in-process pins, on-disk cache pins).

Scope: the Guild issuing profile (eddsa-jcs-2022 over did:key Ed25519,
proofPurpose assertionMethod). Not a general W3C Data Integrity verifier.
Synthetic keys only; no network; no live identity.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agentguild_trustplane import verify as V
from agentguild_trustplane.cache import SignedDecisionCache
from agentguild_trustplane.client import GuildClient

from test_client_transport_offline import FakeGuild, Issuer, fixture

NOW = datetime.now(timezone.utc)


def vc(iss: Issuer, *, start: datetime, end: datetime, subject="did:key:zSubject",
       issuer_field=None, **overrides) -> dict:
    doc = {
        "@context": ["https://www.w3.org/ns/credentials/v2"],
        "id": "urn:passport:agent_1:1",
        "type": ["VerifiableCredential", "AgentGuildPassport"],
        "issuer": iss.did if issuer_field is None else issuer_field,
        "validFrom": start.isoformat(), "validUntil": end.isoformat(),
        "credentialSubject": {"id": subject, "trust": 40.0},
    }
    doc.update(overrides)
    return iss.sign(doc)


# --- positive compatibility controls ---------------------------------------------
def test_live_passport_and_issuer_fixture_still_verify():
    doc = fixture("live_passport_2026-09-08.json")["body"]
    v = V.verify_data_integrity(doc)
    assert v == {"verified": True, "reason": "ok",
                 "issuer_did": fixture("live_issuer_2026-09-08.json")["body"]["did"]}
    captured = datetime.fromisoformat(fixture("live_passport_2026-09-08.json")["captured_at"])
    valid, age = V.within_validity(doc, now=captured + timedelta(minutes=5))
    assert valid and 0 < age < 3600
    # pinned-issuer path (explicit policy, distinct from cryptographic verification)
    assert V.verify_data_integrity(doc, expected_issuer_did=v["issuer_did"])["verified"]
    assert not V.verify_data_integrity(doc, expected_issuer_did="did:key:zOther")["verified"]


def test_decision_envelope_shape_still_verifies():
    iss = Issuer()
    env = iss.sign({"type": "AgentGuildDecision", "issuer": iss.did,
                    "capability": "x", "issued_at": (NOW - timedelta(seconds=5)).isoformat(),
                    "valid_until": (NOW + timedelta(hours=1)).isoformat(),
                    "decision": {"agent_id": "a"}, "routing": {"routable": False}})
    assert V.verify_data_integrity(env)["verified"]
    valid, age = V.within_validity(env)
    assert valid and 0 <= age < 60
    # no issuer field at all (older envelopes): controller from the proof only
    env2 = iss.sign({"type": "AgentGuildDecision", "issued_at": env["issued_at"],
                     "valid_until": env["valid_until"]})
    assert V.verify_data_integrity(env2)["issuer_did"] == iss.did
    # verificationMethod without a fragment is inside the profile
    env3 = {k: v for k, v in env.items() if k != "proof"}
    proof = {"type": "DataIntegrityProof", "cryptosuite": "eddsa-jcs-2022",
             "created": "2026-09-08T00:00:00Z", "verificationMethod": iss.did,
             "proofPurpose": "assertionMethod"}
    sig = iss.sk.sign(V._hash_data(env3, proof))
    assert V.verify_data_integrity({**env3, "proof": {**proof, "proofValue": "z" + V.b58encode(sig)}})["verified"]


# --- validity window -------------------------------------------------------------------
def test_future_start_is_not_valid_yet():
    iss = Issuer()
    doc = vc(iss, start=NOW + timedelta(hours=1), end=NOW + timedelta(days=7))
    valid, age = V.within_validity(doc)
    assert valid is False and age < -3000          # was True on b341a475
    # small issuer clock lead is tolerated, larger is not
    assert V.within_validity(vc(iss, start=NOW + timedelta(seconds=30), end=NOW + timedelta(days=1)))[0]
    assert not V.within_validity(vc(iss, start=NOW + timedelta(seconds=120), end=NOW + timedelta(days=1)))[0]


@pytest.mark.parametrize("start,end", [
    (NOW - timedelta(days=8), NOW - timedelta(days=1)),          # expired
    (NOW + timedelta(days=1), NOW - timedelta(days=1)),          # end before start
])
def test_expired_or_inverted_window_invalid(start, end):
    valid, _ = V.within_validity(vc(Issuer(), start=start, end=end))
    assert valid is False


@pytest.mark.parametrize("patch", [
    {"validFrom": "not-a-date"}, {"validUntil": "not-a-date"},
    {"validFrom": "2026-09-08T00:00:00"},                    # naive
    {"validUntil": None}, {"validFrom": 12345}, {"validUntil": ""},
])
def test_malformed_window_is_invalid_with_no_age(patch):
    iss = Issuer()
    base = {k: v for k, v in vc(iss, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1)).items() if k != "proof"}
    base.update(patch)
    doc = {k: v for k, v in base.items() if v is not None}
    assert V.within_validity(doc) == (False, None)


# --- issuer <-> signing controller binding ----------------------------------------------
def test_document_issuer_must_be_the_signing_controller():
    iss = Issuer()
    doc = vc(iss, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1),
             issuer_field=Issuer().did)                       # names a different issuer
    v = V.verify_data_integrity(doc)
    assert v["verified"] is False                             # was True on b341a475
    assert "not the signing controller" in v["reason"] and v["issuer_did"] == iss.did
    # VC issuer object form is bound the same way
    doc2 = vc(iss, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1),
              issuer_field={"id": iss.did, "name": "Agent Guild"})
    assert V.verify_data_integrity(doc2)["verified"]
    doc3 = vc(iss, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1),
              issuer_field={"id": "did:key:zSomeoneElse"})
    assert not V.verify_data_integrity(doc3)["verified"]


@pytest.mark.parametrize("proof_patch,reason", [
    ({"proofPurpose": "authentication"}, "proofPurpose"),
    ({"proofPurpose": None}, "proofPurpose"),
    ({"verificationMethod": "did:web:example.com#key-1"}, "not a did:key"),
    ({"verificationMethod": "did:key:zNotTheKey"}, "malformed"),
    ({"verificationMethod": None}, "not a did:key"),
    ({"verificationMethod": "FRAGMENT"}, "fragment does not name the did:key"),
    ({"@context": ["https://example.com/other"]}, "@context mismatch"),
    ({"cryptosuite": "ecdsa-jcs-2019"}, "unsupported proof suite"),
    ({"proofValue": "u-not-base58"}, "base58btc"),
    ({"proofValue": None}, "malformed"),
])
def test_malformed_proof_shapes_are_rejected(proof_patch, reason):
    iss = Issuer()
    doc = vc(iss, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1))
    proof = dict(doc["proof"])
    for k, val in proof_patch.items():
        if k == "verificationMethod" and val == "FRAGMENT":
            val = iss.did + "#zWrongFragment"
        if val is None:
            proof.pop(k, None)
        else:
            proof[k] = val
    v = V.verify_data_integrity({**doc, "proof": proof})
    assert v["verified"] is False and reason in v["reason"]


def test_ed25519_only_did_key():
    with pytest.raises(ValueError):
        V.public_key_from_did("did:key:z2DrjgbL9nJcSMWXVX4NRr9w3iUV2g4AQvA6VZzXhB6R5ndqf")  # not ed25519 header
    with pytest.raises(ValueError):
        V.public_key_from_did("did:key:uABC")


# --- an invalid credential never pins an issuer -----------------------------------------
def test_rejected_credentials_do_not_establish_in_process_pin():
    iss = Issuer()
    g = FakeGuild()
    try:
        c = GuildClient(g.base)
        # future start, then subject mismatch, then wrong document issuer
        for doc, requested in [
            (vc(iss, start=NOW + timedelta(hours=1), end=NOW + timedelta(days=7)), "did:key:zSubject"),
            (vc(iss, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1)), "did:key:zOther"),
            (vc(iss, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1), issuer_field="did:key:zX"), "did:key:zSubject"),
        ]:
            g.json("/agents/", 200, doc)
            r = c.passport_result(requested)
            assert not r.ok and r.channel == "unverified", r
            assert c._local_pins == []                     # nothing pinned
        # the first ACCEPTED credential pins; a later different issuer is then refused
        g.json("/agents/", 200, vc(iss, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1)))
        assert c.passport_result("did:key:zSubject").ok and c._local_pins == [iss.did]
        other = Issuer()
        g.json("/agents/", 200, vc(other, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1)))
        r = c.passport_result("did:key:zSubject")
        assert not r.ok and "issuer not allowed" in r.reason and c._local_pins == [iss.did]
    finally:
        g.close()


def test_rejected_documents_do_not_establish_cache_pin(tmp_path):
    iss = Issuer()
    cache = SignedDecisionCache(tmp_path / "c")
    future = vc(iss, start=NOW + timedelta(hours=1), end=NOW + timedelta(days=7))
    assert cache.put("passport", "x", future) is False
    assert cache.trusted_issuers == []
    expired = vc(iss, start=NOW - timedelta(days=9), end=NOW - timedelta(days=2))
    assert cache.put("passport", "x", expired) is False and cache.trusted_issuers == []
    # a future-dated document written behind the cache's back is never served or pinned
    p = cache._path("passport", "y")
    p.write_text(json.dumps({"stored_at": 0, "doc": future}))
    assert cache.get("passport", "y") == (None, "corrupt", None)
    assert cache.trusted_issuers == []
    pins_file = [f for f in (tmp_path / "c").rglob("*") if "pin" in f.name.lower()]
    assert all("did:key" not in f.read_text() for f in pins_file if f.is_file())
    # a valid document pins; a stale one written later is still served as stale (unchanged behaviour)
    assert cache.put("passport", "z", vc(iss, start=NOW - timedelta(days=1), end=NOW + timedelta(days=1)))
    assert cache.trusted_issuers == [iss.did]
    cache._path("passport", "w").write_text(json.dumps({"stored_at": 0, "doc": expired}))
    doc, state, age = cache.get("passport", "w")
    assert doc is not None and state == "stale" and age > 0


def test_signed_decision_pins_only_after_full_verification():
    iss = Issuer()
    g = FakeGuild()
    try:
        c = GuildClient(g.base)
        env = iss.sign({"type": "AgentGuildDecision", "issuer": iss.did, "capability": "x",
                        "issued_at": (NOW + timedelta(hours=1)).isoformat(),
                        "valid_until": (NOW + timedelta(hours=2)).isoformat()})
        g.json("/check", 200, env)
        doc, channel, _ = c.signed_decision("x")
        assert doc is None and channel == "unverified" and "validity" in c.last_verify_failure
        assert c._local_pins == []
    finally:
        g.close()
