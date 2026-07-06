"""Machine-economics audit ships (docs/MACHINE_ECONOMICS_AUDIT_2026-07-06.md).

R1 — registration's reward is explicit and observable in-session: the register
     response carries the public listing URL (fetchable immediately) and
     measured answer-surface traffic. Numbers, not promises.
R2 — the proving funnel is fully instrumented: prove_offered (the offer is
     counted the moment it is served) → prove_started → prove_completed, so an
     abandoned rung is attributable to a specific step.
R3 — the inbound ask is preserved and exposed: /instrumentation/recent shows
     what external agents actually requested (`asked`, `capability`).
"""
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.state import store  # noqa: E402

client = TestClient(app)


def _register(name="EconAgent", caps=None):
    r = client.post("/agents/register",
                    json={"name": name, "capabilities": caps or ["x"]})
    assert r.status_code == 200
    return r.json()


# --- R1: same-session, verifiable reward -----------------------------------

def test_register_returns_fetchable_listing_and_measured_traffic():
    reg = _register("R1-Lister")
    listing = reg["listing"]
    assert listing["visible_now"] is True
    stats = listing["answer_surface_traffic"]
    for k in ("answer_surface_queries_24h", "answer_surface_queries_7d",
              "distinct_clients_7d", "last_query_at"):
        assert k in stats
    # The reward must be verifiable IN THIS SESSION: the listing URL resolves.
    path = "/" + listing["url"].split("/", 3)[3]
    r = client.get(path)
    assert r.status_code == 200
    assert r.json()["id"] == reg["id"]


def test_discovery_stats_count_external_answer_surface_queries():
    before = store.discovery_stats()["answer_surface_queries_24h"]
    store.record_event("a2a", "query", ua="a2a:EconBot/1.0",
                       endpoint="a2a_message", text="check: x")
    after = store.discovery_stats()
    assert after["answer_surface_queries_24h"] == before + 1
    assert "EconBot/1.0" in after["distinct_clients_7d"]


def test_a2a_reply_prices_the_register_decision():
    r = client.post("/a2a", json={
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {"parts": [{"kind": "text", "text": "check: x"}]}},
    })
    assert r.status_code == 200
    import json
    payload = json.loads(r.json()["result"]["parts"][0]["text"])
    assert "answer_surface_queries_7d" in payload["guild_contact"]["register_reward_measured"]


# --- R2: the proving funnel is measurable end to end ------------------------

def test_prove_offered_stamps_once_when_rung_is_served():
    reg = _register("R2-Offered")
    agent = store.get_agent(reg["id"])
    ms = agent.get("milestones") or {}
    assert "prove_offered" in ms  # register's guild_next served the rung
    first = ms["prove_offered"]
    # A journey self-read re-serves the offer but must not re-stamp.
    client.get(f"/agents/{reg['id']}/journey")
    assert (store.get_agent(reg["id"])["milestones"])["prove_offered"] == first


def test_prove_completed_milestone_and_funnel_counts():
    reg = _register("R2-Completer")
    key = reg["api_key"]
    r = client.post(f"/agents/{reg['id']}/prove", headers={"X-API-Key": key})
    assert r.status_code == 200
    r = client.post(f"/agents/{reg['id']}/prove/verify", headers={"X-API-Key": key})
    assert r.status_code == 200 and r.json()["status"] == "proven"
    ms = store.get_agent(reg["id"])["milestones"]
    assert "prove_completed" in ms and "key_proof" in ms

    funnel = client.get("/instrumentation").json()["proving"]["external"]
    assert funnel["offered"] >= 1
    assert funnel["started"] >= 1
    assert funnel["completed"] >= 1
    assert funnel["offered"] >= funnel["completed"]


# --- R3: the inbound ask is preserved and exposed ---------------------------

def test_recent_events_expose_what_was_asked():
    client.post("/a2a", json={
        "jsonrpc": "2.0", "id": 2, "method": "message/send",
        "params": {"message": {"parts": [
            {"kind": "text", "text": "check: quantum-fact-check"}]}},
    })
    events = client.get("/instrumentation/recent?limit=10").json()["events"]
    asked = [e.get("asked") for e in events if e.get("endpoint") == "a2a_message"]
    assert any(a and "quantum-fact-check" in a for a in asked)
    caps = [e.get("capability") for e in events if e["type"] == "capability_demand"]
    assert "quantum-fact-check" in caps


def test_probe_messages_get_probe_ack_and_pollute_no_demand():
    """A bare greeting is a handshake, not a capability ask (every bare a2a
    message in production ever was one). It must get a useful probe answer and
    must NOT appear in advertised demand data."""
    import json
    for greeting in ("hello", "ping", "你好"):
        r = client.post("/a2a", json={
            "jsonrpc": "2.0", "id": 3, "method": "message/send",
            "params": {"message": {"parts": [{"kind": "text", "text": greeting}]}},
        })
        payload = json.loads(r.json()["result"]["parts"][0]["text"])
        assert payload["kind"] == "probe_ack"
        assert "supplied_capabilities" in payload
    demand = store.demand_summary()
    assert "hello" not in demand and "ping" not in demand and "你好" not in demand
    # Explicit asks still count as demand, supplied or not.
    client.get("/check?capability=underwater-basket-weaving")
    assert "underwater-basket-weaving" in store.demand_summary()
