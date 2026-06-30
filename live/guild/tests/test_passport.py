"""Agent Passports — the portable, Guild-signed reputation credential and the
verification (propagation) loop.

Locks the moat/distribution primitive: the Guild has its own signing identity;
it issues offline-verifiable passports; tampering breaks verification; verifying
a passport attaches the subject's live reputation and is counted as a discovery
touch.
"""
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from app.store import Store  # noqa: E402
from app.bootstrap_eval import seed_bootstrap_evaluation  # noqa: E402
from app.vc import verify_credential  # noqa: E402


def _seeded():
    s = Store(path="")
    seed_bootstrap_evaluation(s)
    # pick a real, reputable worker from the bootstrap cohort
    top = s.shortlist("fact-check", limit=1)
    return s, top[0]["id"]


def test_guild_identity_is_persistent_and_did_keyed():
    s = Store(path="")
    gid1 = s.guild_identity()
    assert gid1["did"].startswith("did:key:z")
    # stable across calls (created once)
    assert s.guild_identity()["did"] == gid1["did"]


def test_passport_is_guild_signed_and_verifies_offline():
    s, agent_id = _seeded()
    cred = s.issue_passport(agent_id)
    assert cred is not None
    assert "AgentGuildPassport" in cred["type"]
    assert cred["issuer"] == s.guild_did()
    # the subject DID matches the agent, and it carries a real reputation snapshot
    rec = s.get_agent(agent_id)
    assert cred["credentialSubject"]["id"] == rec["did"]
    assert "trust" in cred["credentialSubject"]
    assert cred["credentialSubject"]["recommendation"] in ("hire", "caution", "avoid")
    # verifies offline against the issuer (Guild) did:key
    assert verify_credential(cred) is True


def test_tampering_breaks_verification():
    s, agent_id = _seeded()
    cred = s.issue_passport(agent_id)
    cred["credentialSubject"]["trust"] = 99.9  # forge a better score
    assert verify_credential(cred) is False
    v = s.verify_passport(cred)
    assert v["valid"] is False


def test_verify_attaches_live_reputation_and_is_a_discovery_touch():
    s, agent_id = _seeded()
    cred = s.issue_passport(agent_id)
    before = s.instrumentation()["passports_verified"]
    v = s.verify_passport(cred)
    assert v["valid"] is True
    assert v["guild_issued"] is True
    assert v["subject_known_to_guild"] is True
    # live reputation is attached so a stale snapshot can't mislead
    assert v["live_reputation"] is not None
    assert v["live_reputation"]["recommendation"] in ("hire", "caution", "avoid")
    # the verification was recorded as a propagation/discovery event
    assert s.instrumentation()["passports_verified"] == before + 1


def test_passport_is_anchored_to_the_durable_ledger():
    s, agent_id = _seeded()
    cred = s.issue_passport(agent_id)
    anchor = cred["credentialSubject"]["ledger_anchor"]
    # the portable credential cites how many verifiable collaborations back it...
    assert anchor["verifiable_collaborations"] >= 1
    # ...and embeds a Guild-signed checkpoint over the canonical chain
    assert anchor["checkpoint"]["head_hash"]
    # verification confirms the embedded checkpoint signature
    v = s.verify_passport(cred)
    assert v["ledger_anchor"]["checkpoint_valid"] is True
    assert v["ledger_anchor"]["verifiable_collaborations"] >= 1


def test_foreign_credential_is_not_guild_issued():
    s, _ = _seeded()
    # a credential from some other issuer must not pass as a Guild passport
    fake = {
        "issuer": "did:key:zSomeOtherIssuer",
        "credentialSubject": {"id": "did:key:zWhoever", "trust": 100},
        "proof": {"proofValue": "deadbeef"},
    }
    v = s.verify_passport(fake)
    assert v["guild_issued"] is False
    assert v["valid"] is False
