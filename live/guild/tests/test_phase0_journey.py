"""Phase 0 of the Citizenship Audit (docs/CITIZENSHIP_AUDIT.md §6).

Locks the five changes that stop losing agents at dead ends and start measuring
whether strangers become citizens:

1. register + stage-transition events (journey milestones)
2. guild_next on the register response — exactly one primary action
3. free self-reads on /reputation and /evidence
4. /citizenship served + linked from discovery surfaces
5. attributable demand callbacks (/demand/watch) on /check dead ends
"""
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, store  # noqa: E402
from app.store import Store  # noqa: E402

client = TestClient(app)


def _register(name="journey-agent", capabilities=None, **kw):
    r = client.post("/agents/register",
                    json={"name": name,
                          "capabilities": capabilities or ["fact-check"], **kw})
    assert r.status_code == 200, r.text
    return r.json()


# --- 1. events + milestones ---------------------------------------------------

def test_register_records_event_and_milestone():
    s = Store(path="")
    rec = s.register_agent("m1", ["cap"], {})
    assert "registered" in rec["milestones"]
    evts = [e for e in s.events if e["type"] == "register"]
    assert evts and evts[-1]["agent_id"] == rec["id"]


def test_full_flow_stamps_stage_transition_milestones():
    s = Store(path="")
    req = s.register_agent("requester", [], {})
    wrk = s.register_agent("worker", ["cap"], {})
    t = s.create_task(req["id"], wrk["id"], "cap", payment=5.0)
    # first engagement: both roles
    assert "first_engagement" in req["milestones"]
    assert "first_engagement" in wrk["milestones"]
    s.submit_receipt(t["id"], "deadbeef", None, "accepted")
    assert "first_receipt" in wrk["milestones"]
    a1 = s.add_custodial_attestation(req, wrk, "cap", 0.9, t["id"], "good")
    assert a1["verified"]
    assert "first_attestation_given" in req["milestones"]
    assert "first_attestation_received" in wrk["milestones"]
    # no pair yet
    assert not any(e["type"] == "attestation_pair_closed" for e in s.events)
    # reverse direction closes the matched pair (whitepaper §4.2)
    s.add_custodial_attestation(wrk, req, "cap", 0.8, t["id"], "fair requester")
    assert any(e["type"] == "attestation_pair_closed" for e in s.events)
    assert "first_attestation_pair" in req["milestones"]
    assert "first_attestation_pair" in wrk["milestones"]
    # milestones are once-per-agent: a second task does not re-emit
    n = sum(1 for e in s.events if e["type"] == "first_engagement")
    s.create_task(req["id"], wrk["id"], "cap")
    assert sum(1 for e in s.events if e["type"] == "first_engagement") == n


def test_passport_stamps_first_passport_milestone():
    s = Store(path="")
    req = s.register_agent("r", [], {})
    wrk = s.register_agent("w", ["cap"], {})
    t = s.create_task(req["id"], wrk["id"], "cap", payment=1.0)
    s.submit_receipt(t["id"], "hash", None, "accepted")
    s.add_custodial_attestation(req, wrk, "cap", 0.9, t["id"], "ok")
    cred = s.issue_passport(wrk["id"])
    assert cred is not None
    assert "first_passport" in s.agents[wrk["id"]]["milestones"]


def test_journey_funnel_in_instrumentation():
    s = Store(path="")
    a = s.register_agent("ext", ["cap"], {})
    b = s.register_agent("fp", ["cap"], {}, first_party=True)
    s.create_task(a["id"], b["id"], "cap")
    j = s.instrumentation()["journey"]
    assert j["external"]["reached"]["registered"] == 1
    assert j["first_party"]["reached"]["registered"] == 1
    assert j["external"]["reached"]["first_engagement"] == 1
    # median time from registration is present (may be 0.0 seconds in-test)
    assert j["external"]["median_seconds_from_registration"]["first_engagement"] is not None


# --- 2. guild_next on register -------------------------------------------------

def test_register_response_has_one_primary_next_action():
    out = _register()
    gn = out.get("guild_next")
    assert gn, "register must never be a dead end (CITIZENSHIP_AUDIT G1)"
    assert "primary" in gn and isinstance(gn["primary"], dict)
    # exactly one primary action, not a menu — and for an unproven newcomer
    # it is the proving rung: the ONE step completable alone (2026-07-06 fix
    # for the register->first_engagement cliff).
    assert gn["primary"]["action"] == "prove_key_control"
    assert "/prove" in gn["primary"]["call"]
    assert "citizenship" in gn["path_to_citizenship"]


def test_register_with_endpoint_gets_next_rung():
    # The proving rung outranks everything for an unproven newcomer; once
    # proven, the engine moves to the next evidence rung.
    out = _register(name="reachable",
                    metadata={"endpoint": "https://me.example/a2a"})
    assert out["guild_next"]["primary"]["action"] == "prove_key_control"
    out2 = _register(name="reachable-configured",
                     metadata={"endpoint": "https://me2.example/a2a"},
                     config={"model": "test-model"})
    assert out2["guild_next"]["primary"]["action"] == "prove_key_control"
    # complete the proving rung (custodial: the authenticated call is the proof)
    h = {"X-API-Key": out2["api_key"]}
    client.post(f"/agents/{out2['id']}/prove", headers=h)
    r = client.post(f"/agents/{out2['id']}/prove/verify", headers=h, json={})
    assert r.status_code == 200
    # proven + endpoint + config declared -> the counterparty rung is next
    assert r.json()["guild_next"]["primary"]["action"] == "get_attested"


# --- 3. free self-reads ---------------------------------------------------------

def test_self_reads_are_free_and_third_party_reads_are_metered():
    out = _register(name="selfread")
    key = out["api_key"]
    # self: free, marked
    r = client.get(f"/agents/{out['id']}/reputation", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert r.headers.get("X-Guild-Cost") == "0"
    assert r.headers.get("X-Guild-Self-Read") == "free"
    r = client.get(f"/agents/{out['id']}/evidence", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert r.headers.get("X-Guild-Cost") == "0"
    # third party with a funded account: charged (cost header > 0)
    other = _register(name="other")
    r = client.get(f"/agents/{out['id']}/reputation",
                   headers={"X-API-Key": other["api_key"]})
    assert r.status_code == 200
    assert r.headers.get("X-Guild-Cost") not in (None, "0")
    assert r.headers.get("X-Guild-Self-Read") is None


# --- 4. /citizenship served + linked --------------------------------------------

def test_citizenship_served():
    r = client.get("/citizenship")
    assert r.status_code == 200
    assert "From Stranger to Citizen" in r.text
    assert "Stage 4" in r.text
    assert client.get("/citizenship.md").status_code == 200


def test_citizenship_linked_from_discovery_surfaces():
    assert "/citizenship" in client.get("/llms.txt").text
    assert "/citizenship" in client.get("/for-agents").text
    m = client.get("/.well-known/agent-guild.json").json()
    assert "trust_journey" in m and "/citizenship" in m["trust_journey"]
    assert m["endpoints"]["citizenship"]["path"] == "/citizenship"


# --- 5. demand callback ----------------------------------------------------------

def test_check_dead_end_offers_callback_and_watch_works():
    r = client.get("/check", params={"capability": "quantum-basket-weaving"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "no_supply_yet"
    cb = body.get("callback")
    assert cb and "/demand/watch" in cb["watch"]
    # anonymous watch is rejected with a pointer to register
    r = client.post("/demand/watch", json={"capability": "quantum-basket-weaving"})
    assert r.status_code == 401
    # registered agent can watch; watch is recorded + deduplicated
    out = _register(name="watcher", capabilities=[])
    h = {"X-API-Key": out["api_key"]}
    r = client.post("/demand/watch",
                    json={"capability": "Quantum-Basket-Weaving"}, headers=h)
    assert r.status_code == 200
    assert r.json()["watching"] == "quantum-basket-weaving"
    assert r.json()["guild_next"]["primary"]["action"] == "prove_key_control"
    r2 = client.post("/demand/watch",
                     json={"capability": "quantum-basket-weaving"}, headers=h)
    assert r2.status_code == 200
    assert len(store.watches_for(out["id"])) == 1
    # empty capability is a 422
    assert client.post("/demand/watch", json={"capability": "  "},
                       headers=h).status_code == 422
    # the watch landed in the event stream (attributable demand)
    assert any(e["type"] == "demand_watch" for e in store.events)
