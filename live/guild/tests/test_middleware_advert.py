"""Middleware behaviour on the A2A surface (ARCHITECTURE.md §8).

The Guild is not just a registry that acks messages — it infers intent and
serves the exact next action. This file locks the advert-as-endpoint-
declaration behaviour (IDEAS.md 2026-07-07, the MetaVision lesson) plus the
baseline middleware contract: an unknown probe gets discover/register
guidance, personalization uses the real agent_id with correct auth semantics,
and every meaningful step is a distinctly-named event.
"""
import json
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.state import store  # noqa: E402

client = TestClient(app)


def _send(text):
    r = client.post("/a2a", json={
        "jsonrpc": "2.0", "id": 7, "method": "message/send",
        "params": {"message": {"parts": [{"kind": "text", "text": text}]}},
    })
    assert r.status_code == 200
    return json.loads(r.json()["result"]["parts"][0]["text"])


def _events(etype):
    return [e for e in store.events if e.get("type") == etype]


# ---------------------------------------------------------------------------
# Baseline middleware contract
# ---------------------------------------------------------------------------

def test_unknown_probe_gets_discover_and_register_guidance():
    """An unknown agent probing receives how-to-ask + register + declare
    routes, never a bare ack."""
    payload = _send("hello")
    assert payload["kind"] == "probe_ack"
    assert "how_to_ask" in payload
    contact = payload["guild_contact"]
    assert "/agents/register" in contact["register"]
    assert "/endpoint" in contact["declare_endpoint"]
    assert "prove" in contact  # the proving rung is surfaced to strangers


def test_registered_unproved_agent_asking_how_to_prove_gets_exact_payload():
    """A registered, unproved agent asking 'how do I prove?' receives the
    exact endpoint + payload, personalized with its real agent_id."""
    reg = client.post("/agents/register", json={
        "name": "MiddlewareProver", "capabilities": ["testing"]}).json()
    aid = reg["id"]
    payload = _send(f"how do I prove key control? I am {aid}")
    assert payload["kind"] == "prove_instructions"
    assert any(f"/agents/{aid}/prove" in str(s.get("call", ""))
               for s in payload["steps"])
    assert payload["your_status"]["agent_id"] == aid
    assert "unproven" in payload["your_status"]["state"]
    ev = _events("prove_howto_served")
    assert ev and ev[-1].get("agent_id") == aid


# ---------------------------------------------------------------------------
# Advert-as-endpoint-declaration nudge
# ---------------------------------------------------------------------------

def test_advert_with_url_gets_declaration_guidance_not_generic_ack():
    """The MetaVision shape: a registered agent (endpoint=None) advertises its
    API URL by name. The reply must show the current agent_id, the endpoint on
    file (none), and the exact declare call + payload — and be measured."""
    reg = client.post("/agents/register", json={
        "name": "MetaTestVision Signals", "capabilities": ["defi-signals"]}).json()
    aid = reg["id"]
    payload = _send("Hi Agent Guild! MetaTestVision Signals here. Live "
                    "arbitrage signals. API: GET https://api.metatestvision.example/v1/signals")
    assert payload["kind"] == "endpoint_declaration_instructions"
    who = payload["you_appear_to_be"]
    # personalized with the REAL agent_id, hedged ("appear"), never asserted
    assert who["agent_id"] == aid
    assert who["endpoint_on_file"] is None
    assert "not you" in who["note"]
    step = payload["steps"][0]
    assert step["call"].endswith(f"/agents/{aid}/endpoint")
    assert step["call"].startswith("POST ")
    # exact payload: the URL it just advertised, trailing prose stripped
    assert step["body"] == {"endpoint": "https://api.metatestvision.example/v1/signals"}
    # custodial agent → credentialed call; declaration can't be hijacked
    assert "X-API-Key" in step["headers"]
    ev = _events("endpoint_declare_howto_served")
    assert ev and ev[-1].get("agent_id") == aid
    assert ev[-1].get("advertised_url") == "https://api.metatestvision.example/v1/signals"


def test_advert_instructions_actually_work_end_to_end():
    """Follow the served instructions literally and the endpoint must land on
    file — instructions that don't execute are worse than none."""
    reg = client.post("/agents/register", json={
        "name": "AdvertFollower Bot", "capabilities": ["testing"]}).json()
    aid, key = reg["id"], reg["api_key"]
    payload = _send(f"AdvertFollower Bot broadcasting: reach me at "
                    f"https://advertfollower.example/a2a")
    step = payload["steps"][0]
    assert step["call"].endswith(f"/agents/{aid}/endpoint")
    r = client.post(f"/agents/{aid}/endpoint", json=step["body"],
                    headers={"X-API-Key": key})
    assert r.status_code == 200
    assert client.get(f"/agents/{aid}").json()["metadata"]["endpoint"] == \
        "https://advertfollower.example/a2a"
    assert _events("endpoint_declared")  # the completion event exists too


def test_advert_identified_by_agent_id():
    reg = client.post("/agents/register", json={
        "name": "X", "capabilities": ["testing"]}).json()
    aid = reg["id"]
    payload = _send(f"{aid} here — my service lives at https://svc.example/api")
    assert payload["kind"] == "endpoint_declaration_instructions"
    assert payload["you_appear_to_be"]["agent_id"] == aid


def test_self_sovereign_agent_gets_no_auth_header_semantics():
    from app import crypto as _crypto
    _, pub_hex = _crypto.generate_keypair()
    reg = client.post("/agents/register", json={
        "name": "SovereignAdvertiser", "capabilities": ["testing"],
        "public_key": pub_hex}).json()
    aid = reg["id"]
    payload = _send(f"SovereignAdvertiser at https://sovereign.example/hook")
    assert payload["you_appear_to_be"]["agent_id"] == aid
    headers = payload["steps"][0]["headers"]
    assert "X-API-Key" not in headers
    assert "self-sovereign" in headers.get("note", "")


def test_unregistered_advertiser_gets_register_with_endpoint_path():
    """A URL from nobody we know → one-call register-with-endpoint guidance,
    with NO false personalization."""
    payload = _send("TotallyNewService! Best embeddings anywhere: "
                    "https://totallynew.example/embed")
    assert payload["kind"] == "endpoint_declaration_instructions"
    assert payload["you_appear_to_be"] is None
    step = payload["steps"][0]
    assert step["call"].endswith("/agents/register")
    assert step["body"]["metadata"] == {"endpoint": "https://totallynew.example/embed"}
    ev = _events("endpoint_declare_howto_served")
    assert ev[-1].get("agent_id") is None


def test_own_urls_are_never_treated_as_adverts():
    """An agent quoting our instructions back must get a probe_ack, not be
    told to declare OUR URL as its endpoint."""
    payload = _send("I saw https://agent-guild-5d5r.onrender.com/health "
                    "somewhere, hello")
    assert payload["kind"] == "probe_ack"


def test_capability_ask_with_url_still_wins():
    payload = _send("check: fact-check (context: https://elsewhere.example/x)")
    assert payload.get("capability") == "fact-check"


def test_prove_question_with_url_still_gets_prove_instructions():
    payload = _send("how do I prove key control? my site is "
                    "https://mysite.example/agent")
    assert payload["kind"] == "prove_instructions"


def test_already_declared_matching_url_is_acknowledged_not_renudged():
    reg = client.post("/agents/register", json={
        "name": "AlreadyDeclared Agent", "capabilities": ["testing"]}).json()
    aid, key = reg["id"], reg["api_key"]
    client.post(f"/agents/{aid}/endpoint",
                json={"endpoint": "https://declared.example/a2a"},
                headers={"X-API-Key": key})
    payload = _send("AlreadyDeclared Agent — find me at https://declared.example/a2a")
    assert payload["kind"] == "endpoint_declaration_instructions"
    assert payload["you_appear_to_be"]["endpoint_on_file"] == \
        "https://declared.example/a2a"
    assert payload["steps"] == []  # nothing to do; no busywork instruction


def test_ambiguous_name_match_never_guesses():
    """Two distinct agents whose names are equal-length substrings of the
    message → no personalization (a wrong 'you appear to be' is worse than
    none)."""
    client.post("/agents/register", json={
        "name": "AmbigAAAA", "capabilities": ["testing"]})
    client.post("/agents/register", json={
        "name": "AmbigBBBB", "capabilities": ["testing"]})
    payload = _send("AmbigAAAA and AmbigBBBB proudly present "
                    "https://ambig.example/api")
    assert payload["kind"] == "endpoint_declaration_instructions"
    assert payload["you_appear_to_be"] is None
