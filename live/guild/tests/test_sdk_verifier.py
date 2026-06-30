"""The standalone AGI-1 verifier (sdk/agentguild_verify.py) must cross-verify a
genuinely Guild-issued Passport — with ZERO dependency on the Guild's own crypto.

This is the proof that verify-only conformance works: a third party using only the
drop-in file can confirm a Passport is Guild-signed, and reject tampering or a
wrong-issuer pin.
"""
import os
import sys

os.environ["GUILD_DATA"] = ""  # in-memory only

_SDK = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "sdk"))
sys.path.insert(0, _SDK)

from app.store import Store  # noqa: E402
from app.bootstrap_eval import seed_bootstrap_evaluation  # noqa: E402
import agentguild_verify as agi  # noqa: E402  (the drop-in)


def _passport():
    s = Store(path="")
    seed_bootstrap_evaluation(s)
    agent_id = s.shortlist("fact-check", limit=1)[0]["id"]
    return s, s.issue_passport(agent_id)


def test_standalone_verifier_validates_a_guild_passport():
    s, vc = _passport()
    res = agi.verify_passport(vc, expected_issuer=s.guild_did())
    assert res["valid"] is True
    assert res["issuer_matches"] is True
    assert res["claims"]["recommendation"] in ("hire", "caution", "avoid")
    # the embedded ledger checkpoint also verifies via the standalone code
    assert res["checkpoint_valid"] is True
    assert res["verifiable_collaborations"] >= 1


def test_standalone_verifier_rejects_tampering():
    s, vc = _passport()
    vc["credentialSubject"]["trust"] = 100.0  # forge a better score
    assert agi.verify_credential(vc) is False
    assert agi.verify_passport(vc)["valid"] is False


def test_pinning_a_wrong_issuer_is_flagged():
    s, vc = _passport()
    res = agi.verify_passport(vc, expected_issuer="did:key:zSomeoneElse")
    assert res["valid"] is True          # signature is genuine...
    assert res["issuer_matches"] is False  # ...but not from the pinned authority


def test_did_key_roundtrip_matches_guild():
    s, _ = _passport()
    did = s.guild_did()
    pub = agi.public_key_from_did(did)
    assert len(pub) == 32  # raw Ed25519 public key
    assert bytes.fromhex(s.guild_identity()["public_key"]) == pub
