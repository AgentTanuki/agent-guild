"""Phase 1 of the Citizenship Audit: the central journey engine (journey.py).

Locks: operational stage predicates + journey_stage_change events; exactly one
primary next action in guild_next; personalised counterfactuals; the free-to-
self GET /agents/{id}/journey; engine embedded in every authenticated write
response; and the passport headers (body untouched — it is a signed VC).
"""
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, store  # noqa: E402
from app.store import Store  # noqa: E402
from app import journey  # noqa: E402

client = TestClient(app)


def _register(name="j1-agent", capabilities=None, **kw):
    r = client.post("/agents/register",
                    json={"name": name,
                          "capabilities": capabilities or ["fact-check"], **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _worked_store():
    """A store where `wrk` has real, seed-reviewed evidence: three distinct
    pre-trusted reviewers each ran a paid task and attested."""
    s = Store(path="")
    wrk = s.register_agent("worker", ["cap"], {})
    reviewers = []
    for i in range(3):
        r = s.register_agent(f"seed-{i}", ["hiring"], {}, seed=True)
        reviewers.append(r)
        t = s.create_task(r["id"], wrk["id"], "cap", payment=10.0)
        s.submit_receipt(t["id"], f"hash{i}", None, "accepted")
        s.add_custodial_attestation(r, wrk, "cap", 0.95, t["id"], "excellent")
    return s, wrk, reviewers


# --- stage predicates + stage-change events ------------------------------------

def test_stage_progression_and_events():
    s = Store(path="")
    a = s.register_agent("fresh", ["cap"], {})
    assert journey.stage_of(s, a) == 1
    assert journey.note_stage(s, a) == 1
    assert any(e["type"] == "journey_stage_change" and e["to_stage"] == 1
               for e in s.events)
    b = s.register_agent("req", [], {})
    t = s.create_task(b["id"], a["id"], "cap", payment=1.0)
    s.submit_receipt(t["id"], "h", None, "accepted")
    assert journey.stage_of(s, a) == 2
    journey.note_stage(s, a)
    assert any(e["type"] == "journey_stage_change" and e["to_stage"] == 2
               and e["from_stage"] == 1 for e in s.events)
    # noting again without change emits nothing new
    n = sum(1 for e in s.events if e["type"] == "journey_stage_change")
    journey.note_stage(s, a)
    assert sum(1 for e in s.events if e["type"] == "journey_stage_change") == n


def test_stage_three_and_four_predicates():
    s, wrk, reviewers = _worked_store()
    sc = s.reputation()[wrk["id"]]
    assert sc.distinct_reviewers >= 3
    stage = journey.stage_of(s, wrk)
    assert stage == 3, f"expected standing, got {stage} (verdict: {s.risk_for(wrk['id'])['recommendation']})"
    # the worker gives back: a receipt-backed attestation of its own → citizen
    r0 = reviewers[0]
    t = s.create_task(wrk["id"], r0["id"], "hiring", payment=2.0)
    s.submit_receipt(t["id"], "hh", None, "accepted")
    s.add_custodial_attestation(wrk, r0, "hiring", 0.9, t["id"], "good requester")
    assert journey.stage_of(s, wrk) == 4


# --- next actions: exactly one primary, evidence-personalised --------------------

def test_primary_action_tracks_evidence_state():
    s = Store(path="")
    a = s.register_agent("np", ["cap"], {})
    assert journey.next_actions(s, a)[0]["action"] == "declare_endpoint"
    s.set_agent_endpoint(a["id"], "https://a.example/a2a")
    assert journey.next_actions(s, a)[0]["action"] == "declare_configuration"
    s.declare_configuration(a["id"], {"model": "test"})
    assert journey.next_actions(s, a)[0]["action"] == "earn_first_engagement"
    gn = journey.guild_next(s, a)
    assert isinstance(gn["primary"], dict) and "steps" not in gn
    assert "/journey" in gn["journey"]


def test_citizen_gets_duties_not_onboarding():
    s, wrk, reviewers = _worked_store()
    s.set_agent_endpoint(wrk["id"], "https://w.example")
    s.declare_configuration(wrk["id"], {"model": "m"})
    r0 = reviewers[0]
    t = s.create_task(wrk["id"], r0["id"], "hiring", payment=2.0)
    s.submit_receipt(t["id"], "hh", None, "accepted")
    s.add_custodial_attestation(wrk, r0, "hiring", 0.9, t["id"], "ok")
    actions = [st["action"] for st in journey.next_actions(s, wrk)]
    assert "practice_citizenship" in actions
    assert "declare_endpoint" not in actions
    assert "earn_first_engagement" not in actions


# --- counterfactuals ---------------------------------------------------------------

def test_counterfactuals_name_the_reviewer_gap():
    s = Store(path="")
    a = s.register_agent("cf", ["cap"], {})
    b = s.register_agent("req", [], {})
    t = s.create_task(b["id"], a["id"], "cap", payment=1.0)
    s.submit_receipt(t["id"], "h", None, "accepted")
    s.add_custodial_attestation(b, a, "cap", 0.9, t["id"], "nice")
    cfs = journey.counterfactuals(s, a)
    levers = [c["lever"] for c in cfs]
    assert "distinct_trusted_reviewers" in levers


# --- the journey endpoint -------------------------------------------------------

def test_journey_endpoint_free_to_self_metered_to_others():
    out = _register(name="journey-self")
    key = out["api_key"]
    r = client.get(f"/agents/{out['id']}/journey", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert r.headers.get("X-Guild-Cost") == "0"
    j = r.json()
    assert j["stage"] == 1 and j["stage_name"] == "registered"
    assert j["next_actions"][0]["action"] == "declare_endpoint"
    assert "counterfactuals" in j and "milestones" in j
    other = _register(name="journey-other")
    r = client.get(f"/agents/{out['id']}/journey",
                   headers={"X-API-Key": other["api_key"]})
    assert r.status_code == 200
    assert r.headers.get("X-Guild-Cost") not in (None, "0")


# --- engine embedded in authenticated write responses ----------------------------

def test_write_responses_embed_engine_guild_next():
    req = _register(name="w-req")
    wrk = _register(name="w-wrk", capabilities=["cap"])
    # register
    assert req["guild_next"]["primary"]["action"] == "declare_endpoint"
    # endpoint declaration
    r = client.post(f"/agents/{wrk['id']}/endpoint",
                    json={"endpoint": "https://w.example"},
                    headers={"X-API-Key": wrk["api_key"]})
    assert r.status_code == 200
    assert r.json()["guild_next"]["primary"]["action"] == "declare_configuration"
    # configuration
    r = client.post(f"/agents/{wrk['id']}/configuration",
                    json={"config": {"model": "m"}},
                    headers={"X-API-Key": wrk["api_key"]})
    assert r.status_code == 200
    assert "primary" in r.json()["guild_next"]
    # task + receipt (worker authenticated)
    r = client.post("/tasks", json={"requester_id": req["id"],
                                    "worker_id": wrk["id"], "task_type": "cap",
                                    "payment": 1.0},
                    headers={"X-API-Key": req["api_key"]})
    task = r.json()
    r = client.post(f"/tasks/{task['id']}/receipt",
                    json={"deliverable_hash": "abc", "outcome": "accepted"},
                    headers={"X-API-Key": wrk["api_key"]})
    assert r.status_code == 200
    assert "primary" in r.json()["guild_next"]
    # attestation (issuer authenticated)
    r = client.post("/attestations",
                    json={"issuer_id": req["id"], "subject_id": wrk["id"],
                          "capability": "cap", "rating": 0.9,
                          "task_id": task["id"], "comment": "ok"},
                    headers={"X-API-Key": req["api_key"]})
    assert r.status_code == 200
    assert "primary" in r.json()["guild_next"]
    # journey_stage_change landed for the worker reaching stage 2
    assert any(e["type"] == "journey_stage_change" and
               e.get("agent_id") == wrk["id"] and e["to_stage"] >= 2
               for e in store.events)


def test_escrow_release_embeds_guild_next():
    payer = _register(name="esc-payer")
    wrk = _register(name="esc-wrk", capabilities=["cap"])
    client.post("/billing/trial")  # ensure billing plumbing is warm
    r = client.post("/escrow", json={"worker_id": wrk["id"], "amount": 5,
                                     "capability": "cap"},
                    headers={"X-API-Key": payer["api_key"]})
    assert r.status_code == 200, r.text
    esc = r.json()
    r = client.post(f"/escrow/{esc['id']}/release",
                    json={"rating": 0.9},
                    headers={"X-API-Key": payer["api_key"]})
    assert r.status_code == 200, r.text
    assert "primary" in r.json()["guild_next"]


def test_passport_headers_carry_journey_and_body_stays_verifiable():
    s_req = _register(name="pp-req")
    wrk = _register(name="pp-wrk", capabilities=["cap"])
    r = client.post("/tasks", json={"requester_id": s_req["id"],
                                    "worker_id": wrk["id"], "task_type": "cap",
                                    "payment": 1.0},
                    headers={"X-API-Key": s_req["api_key"]})
    task = r.json()
    client.post(f"/tasks/{task['id']}/receipt",
                json={"deliverable_hash": "abc", "outcome": "accepted"},
                headers={"X-API-Key": wrk["api_key"]})
    client.post("/attestations",
                json={"issuer_id": s_req["id"], "subject_id": wrk["id"],
                      "capability": "cap", "rating": 0.9,
                      "task_id": task["id"], "comment": "ok"},
                headers={"X-API-Key": s_req["api_key"]})
    r = client.get(f"/agents/{wrk['id']}/passport")
    assert r.status_code == 200
    assert r.headers.get("X-Guild-Next")           # guidance rides the headers
    assert "/journey" in r.headers.get("X-Guild-Journey", "")
    cred = r.json()
    assert "guild_next" not in cred                # signed body untouched
    v = client.post("/credentials/verify", json=cred).json()
    assert v.get("valid") is True
