"""Stage-2: published, pinnable checkpoint feed + passports that cite it
(LEDGER_ARCHITECTURE.md §7). Stage-1 already dual-writes evidence onto the
durable chain; stage-2 adds canonical commitments third parties pin, and makes
passports anchor to the published feed rather than an ephemeral checkpoint."""
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.store import Store  # noqa: E402
from app.bootstrap_eval import seed_bootstrap_evaluation  # noqa: E402
from app.ledger import Ledger  # noqa: E402

client = TestClient(app)


def _seeded():
    s = Store(path="")
    seed_bootstrap_evaluation(s)
    return s, s.shortlist("fact-check", limit=1)[0]["id"]


def test_publish_appends_and_is_signed_and_verifiable():
    s, _ = _seeded()
    entry = s.publish_checkpoint()
    assert entry["index"] == 0
    assert entry["ledger_length"] == len(s.ledger_records)
    cp = entry["checkpoint"]
    assert cp["head_hash"]
    assert Ledger.verify_checkpoint(cp) is True


def test_publish_is_idempotent_without_new_evidence():
    s, _ = _seeded()
    a = s.publish_checkpoint()
    b = s.publish_checkpoint()          # nothing new landed
    assert b["index"] == a["index"]     # no duplicate commitment
    assert len(s.checkpoints) == 1


def test_new_evidence_advances_the_feed():
    s, _ = _seeded()
    s.publish_checkpoint()
    # register a new agent -> a fresh evidence event lands on the chain
    s.register_agent(name="Newcomer", capabilities=["fact-check"], metadata={})
    s.publish_checkpoint()
    assert len(s.checkpoints) == 2
    assert s.checkpoints[1]["index"] == 1


def test_passport_cites_the_published_checkpoint():
    s, agent_id = _seeded()
    published = s.latest_checkpoint()
    cred = s.issue_passport(agent_id)
    anchor = cred["credentialSubject"]["ledger_anchor"]
    assert anchor["checkpoint_index"] == published["index"]
    assert anchor["checkpoint"]["head_hash"] == published["checkpoint"]["head_hash"]


def test_feed_endpoint_and_admin_gated_publish():
    # no admin token configured in tests -> publish is open; feed lists entries
    r = client.post("/ledger/checkpoint/publish")
    assert r.status_code == 200
    feed = client.get("/ledger/checkpoints").json()
    assert feed["count"] >= 1
    assert feed["checkpoints"][0]["checkpoint"]["head_hash"]
