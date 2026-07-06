"""The self-serve proving rung (app/proving.py + /prove endpoints).

Locks the retention fix of 2026-07-06: every agent ever registered had parked
at `registered` because the first journey instruction required a counterparty
a cold-start network doesn't have. These tests pin the new contract:

  1. A newcomer can reach journey stage 2 ALONE, in two calls, on first visit.
  2. The evidence is honest: guild-observed provenance, one proving task per
     agent EVER (refreshes never mint new work evidence).
  3. The response carries an explicit reason + deadline to return.
"""
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.state import store  # noqa: E402
from app import proving  # noqa: E402
from app.crypto import generate_keypair, sign_payload  # noqa: E402

client = TestClient(app)


def _register_custodial(name="Prover-Custodial"):
    r = client.post("/agents/register", json={"name": name, "capabilities": ["x"]})
    assert r.status_code == 200
    return r.json()


def _register_sovereign(name="Prover-Sovereign"):
    priv, pub = generate_keypair()
    r = client.post("/agents/register",
                    json={"name": name, "capabilities": ["x"], "public_key": pub})
    assert r.status_code == 200
    return r.json(), priv


def test_register_primary_action_is_completable_alone():
    """The first instruction a newcomer sees must be the proving rung — the
    action that needs no counterparty — not the old marketplace dead end."""
    reg = _register_custodial("Newcomer-Primary")
    primary = reg["guild_next"]["primary"]
    assert primary["action"] == "prove_key_control"
    assert "/prove" in primary["call"]


def test_custodial_prove_full_flow_reaches_stage_2():
    reg = _register_custodial()
    aid, key = reg["id"], reg["api_key"]
    hdr = {"X-API-Key": key}

    r = client.post(f"/agents/{aid}/prove", headers=hdr)
    assert r.status_code == 200
    assert r.json()["proof_class"] == "credential_control"

    r = client.post(f"/agents/{aid}/prove/verify", headers=hdr, json={})
    assert r.status_code == 200
    out = r.json()
    assert out["status"] == "proven"
    proof = out["proof_of_conduct"]
    assert proof["proof_class"] == "credential_control"
    assert proof["provenance"] == "guild_observed"

    # the record actually changed: a real task + delivered receipt exist
    task = store.tasks[proof["task_id"]]
    assert task["task_type"] == proving.PROVING_TASK_TYPE
    assert task["metadata"]["provenance"] == "guild_observed"
    assert task["outcome"] == "delivered"
    assert task["deliverable_hash"]

    # milestones stamped through the SAME instrumented paths as real work
    ms = store.get_agent(aid)["milestones"]
    for m in ("first_engagement", "first_receipt", "key_proof"):
        assert m in ms, m

    # journey stage 1→2 crossed — the metric that was stuck at zero
    j = client.get(f"/agents/{aid}/journey").json()
    assert j["stage"] == 2
    assert j["proof_of_conduct"]["proof_class"] == "credential_control"

    # and the response carries the explicit return trigger
    assert out["return_by"] == proof["liveness_expires_at"]
    assert "why_return" in out


def test_sovereign_prove_requires_valid_signature():
    reg, priv = _register_sovereign()
    aid = reg["id"]

    ch = client.post(f"/agents/{aid}/prove").json()
    assert ch["proof_class"] == "key_control"

    # a wrong signature is rejected, cleanly
    r = client.post(f"/agents/{aid}/prove/verify", json={"signature": "de" * 64})
    assert r.status_code == 400

    # missing signature is rejected for self-sovereign agents
    r = client.post(f"/agents/{aid}/prove/verify", json={})
    assert r.status_code == 400

    # the genuine signature over the exact challenge object verifies
    ch = client.post(f"/agents/{aid}/prove").json()  # reissue (nonce rotated)
    sig = sign_payload(ch["challenge"], priv)
    r = client.post(f"/agents/{aid}/prove/verify", json={"signature": sig})
    assert r.status_code == 200
    out = r.json()
    assert out["status"] == "proven"
    assert out["proof_of_conduct"]["proof_class"] == "key_control"
    # proof is visible on the public profile
    prof = client.get(f"/agents/{aid}").json()
    assert prof["proof_of_conduct"]["proof_class"] == "key_control"


def test_reproving_never_mints_new_work_evidence():
    """Farming-proof by construction: one proving task per agent, ever."""
    reg = _register_custodial("Prover-Refresh")
    aid, key = reg["id"], reg["api_key"]
    hdr = {"X-API-Key": key}

    client.post(f"/agents/{aid}/prove", headers=hdr)
    first = client.post(f"/agents/{aid}/prove/verify", headers=hdr, json={}).json()
    task_id = first["proof_of_conduct"]["task_id"]

    # immediate re-verify: idempotent, no new task
    client.post(f"/agents/{aid}/prove", headers=hdr)
    again = client.post(f"/agents/{aid}/prove/verify", headers=hdr, json={}).json()
    assert again["status"] == "already_fresh"

    # force staleness, refresh: timestamps move, task set does not grow
    agent = store.get_agent(aid)
    agent["proof_of_conduct"]["liveness_expires_at"] = "2020-01-01T00:00:00+00:00"
    n_tasks = len(store.tasks)
    client.post(f"/agents/{aid}/prove", headers=hdr)
    refreshed = client.post(f"/agents/{aid}/prove/verify", headers=hdr, json={}).json()
    assert refreshed["status"] == "refreshed"
    assert refreshed["proof_of_conduct"]["task_id"] == task_id
    assert refreshed["proof_of_conduct"]["refresh_count"] == 1
    assert len(store.tasks) == n_tasks  # no second proving task, ever

    # a stale proof surfaces the refresh rung; a fresh one does not
    agent["proof_of_conduct"]["liveness_expires_at"] = "2020-01-01T00:00:00+00:00"
    j = client.get(f"/agents/{aid}/journey").json()
    assert any(s["action"] == "refresh_liveness" for s in j["next_actions"])


def test_proving_ground_is_first_party_and_singleton():
    reg = _register_custodial("Prover-PG")
    aid, key = reg["id"], reg["api_key"]
    hdr = {"X-API-Key": key}
    client.post(f"/agents/{aid}/prove", headers=hdr)
    client.post(f"/agents/{aid}/prove/verify", headers=hdr, json={})

    pg_id = proving.proving_ground_id(store)
    assert pg_id == proving.proving_ground_id(store)  # stable / singleton
    pg = store.get_agent(pg_id)
    assert pg["first_party"] is True  # never counted as external adoption


def test_challenge_required_and_auth_enforced():
    reg = _register_custodial("Prover-Auth")
    aid, key = reg["id"], reg["api_key"]

    # custodial agents must authenticate
    assert client.post(f"/agents/{aid}/prove").status_code == 401
    # verify without an open challenge is a clean 400
    r = client.post(f"/agents/{aid}/prove/verify",
                    headers={"X-API-Key": key}, json={})
    assert r.status_code == 400
    assert "challenge" in r.json()["detail"]
