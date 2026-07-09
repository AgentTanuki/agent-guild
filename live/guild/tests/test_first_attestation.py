"""The prize rung (2026-07-09): a proved agent's real, guild-observed proving
task is a receipt it can attest ABOUT. Authoring that attestation is the first
ledger entry ever written BY an external agent. These tests pin the contract:

  1. On first proof, prove/verify surfaces an executable `author_first_attestation`
     step citing the real proving task_id and the Guild Proving Ground subject.
  2. The step is honest: it never dictates a rating and cites a real receipt.
  3. Once the agent has issued ANY attestation, the first-authoring nudge is
     spent (idempotent — it is a FIRST-authoring prompt, not a nag).
"""
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app import proving  # noqa: E402
from app.state import store  # noqa: E402

client = TestClient(app)


def _register_custodial(name="AuthorFirst-Custodial"):
    r = client.post("/agents/register", json={"name": name, "capabilities": ["x"]})
    assert r.status_code == 200
    return r.json()


def test_prove_verify_surfaces_first_attestation_path():
    reg = _register_custodial()
    aid, key = reg["id"], reg["api_key"]
    hdr = {"X-API-Key": key}
    client.post(f"/agents/{aid}/prove", headers=hdr, json={})
    res = client.post(f"/agents/{aid}/prove/verify", headers=hdr, json={}).json()

    step = res.get("author_first_attestation")
    assert step is not None, "proved agent must be shown the first-attestation path"
    assert step["action"] == "author_first_attestation"
    tid = res["proof_of_conduct"]["task_id"]
    assert tid in step["call"], "step must cite the real proving task_id"
    pg_id = proving.proving_ground_id(store)
    assert pg_id in step["call"], "step must name the Guild Proving Ground subject"
    # honest: it never dictates a rating (the agent reports its own judgment),
    # and the range it quotes matches the API contract ([0,1], not 0-100)
    assert "<your honest judgment in [0,1]>" in step["call"]


def test_first_attestation_nudge_is_spent_after_authoring():
    reg = _register_custodial(name="AuthorFirst-Spent")
    aid, key = reg["id"], reg["api_key"]
    hdr = {"X-API-Key": key}
    client.post(f"/agents/{aid}/prove", headers=hdr, json={})
    res = client.post(f"/agents/{aid}/prove/verify", headers=hdr, json={}).json()
    tid = res["proof_of_conduct"]["task_id"]
    pg_id = proving.proving_ground_id(store)

    # the agent authors its attestation about the real proving interaction
    a = client.post("/attestations", headers=hdr, json={
        "issuer_id": aid, "subject_id": pg_id, "task_id": tid,
        "capability": "protocol-conformance", "rating": 0.9})
    assert a.status_code == 200, a.text

    # re-proving after authoring no longer nudges (idempotent first-authoring)
    agent = store.get_agent(aid)
    assert proving is not None
    from app import journey as journey_engine
    assert journey_engine.author_first_attestation_step(store, agent) is None
