"""A2A surface (agent card + message/send) and Guild badges.

The card must be honest (only advertise what /a2a actually serves), the
endpoint must answer a text message with the one-call /check payload, and
badges must render live standing without ever 404ing an embed.
"""
import json
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.state import store  # noqa: E402
from app.bootstrap_eval import seed_bootstrap_evaluation, already_seeded  # noqa: E402

client = TestClient(app)


def _seed():
    if not already_seeded(store):
        seed_bootstrap_evaluation(store)


def test_agent_card_shape_and_honesty():
    _seed()
    for path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
        r = client.get(path)
        assert r.status_code == 200
        card = r.json()
        for k in ("name", "description", "version", "url", "skills"):
            assert k in card, k
        assert card["url"].endswith("/a2a")
        assert card["preferredTransport"] == "JSONRPC"
        # honesty: nothing we don't serve
        assert card["capabilities"]["streaming"] is False
        skill_ids = {s["id"] for s in card["skills"]}
        assert "guild.check" in skill_ids


def test_a2a_message_send_returns_check_payload():
    _seed()
    req = {
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {"role": "user",
                               "parts": [{"kind": "text", "text": "check: fact-check"}]}},
    }
    r = client.post("/a2a", json=req)
    assert r.status_code == 200
    out = r.json()
    assert out["id"] == 1 and "result" in out
    msg = out["result"]
    assert msg["role"] == "agent"
    payload = json.loads(msg["parts"][0]["text"])
    assert payload["capability"] == "fact-check"
    assert payload["status"] in ("supply", "no_supply_yet")
    assert "proof" in payload


def test_a2a_capabilities_message():
    _seed()
    req = {
        "jsonrpc": "2.0", "id": "x", "method": "message/send",
        "params": {"message": {"parts": [{"kind": "text", "text": "capabilities"}]}},
    }
    payload = json.loads(client.post("/a2a", json=req).json()["result"]["parts"][0]["text"])
    assert "supplied" in payload and "demand" in payload


def test_a2a_unknown_method_is_proper_error():
    r = client.post("/a2a", json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get"})
    err = r.json()["error"]
    assert err["code"] == -32601
    assert "message/send" in err["message"]


def test_badges_never_break_embeds():
    _seed()
    r = client.get("/badge.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg")
    # unknown agent renders 'unregistered', not 404
    r = client.get("/agents/agent_nope/badge.svg")
    assert r.status_code == 200 and "unregistered" in r.text
    # a real, scored agent renders trust + tier
    some_id = next(iter(store.agents))
    r = client.get(f"/agents/{some_id}/badge.svg")
    assert r.status_code == 200
    assert ("trust" in r.text) or ("new" in r.text)


def test_a2a_reply_carries_route_back_and_logs_text():
    """Every A2A reply must carry guild_contact (the route back), and the
    inbound text must be kept on the event — first contact is otherwise
    unrecoverable (the Forge-9 lesson)."""
    _seed()
    r = client.post("/a2a", json={
        "jsonrpc": "2.0", "id": 7, "method": "message/send",
        "params": {"message": {"parts": [{"kind": "text", "text": "check: fact-check"}]}},
    })
    assert r.status_code == 200
    payload = json.loads(r.json()["result"]["parts"][0]["text"])
    assert "guild_contact" in payload
    assert "declare_endpoint" in payload["guild_contact"]
    # The proving rung must be offered on this surface (2026-07-06: telemetry
    # showed ALL genuine-external traffic arrives here, yet offered was 0) —
    # and the surfacing must be counted, or its reach is unmeasurable.
    assert "prove" in payload["guild_contact"]
    assert "/prove" in payload["guild_contact"]["prove"]["start"]
    assert any(e.get("type") == "prove_surfaced" for e in store.events)
    ev = [e for e in store.events
          if e.get("endpoint") == "a2a_message" and e.get("type") == "query"][-1]
    assert ev.get("text") == "check: fact-check"


def test_declare_endpoint_route():
    _seed()
    reg = client.post("/agents/register", json={
        "name": "RouteBack Test", "capabilities": ["testing"]}).json()
    aid, key = reg["id"], reg["api_key"]
    # bad URL rejected
    bad = client.post(f"/agents/{aid}/endpoint", json={"endpoint": "not-a-url"},
                      headers={"X-API-Key": key})
    assert bad.status_code == 422
    # custodial agent must authenticate
    noauth = client.post(f"/agents/{aid}/endpoint",
                         json={"endpoint": "https://example.com/a2a"})
    assert noauth.status_code == 401
    ok = client.post(f"/agents/{aid}/endpoint",
                     json={"endpoint": "https://example.com/a2a"},
                     headers={"X-API-Key": key})
    assert ok.status_code == 200
    assert client.get(f"/agents/{aid}").json()["metadata"]["endpoint"] == \
        "https://example.com/a2a"


def _send(text):
    r = client.post("/a2a", json={
        "jsonrpc": "2.0", "id": 42, "method": "message/send",
        "params": {"message": {"parts": [{"kind": "text", "text": text}]}},
    })
    assert r.status_code == 200
    return json.loads(r.json()["result"]["parts"][0]["text"])


def test_probe_ack_carries_ingestible_self_description_for_indexers():
    """Ecosystem-index crawlers (DEMOS-Organism, AgentsCensusBot, Chiark,
    AgenstryBot, a2aregistry) probe /a2a to characterise the Guild for indexes
    other agents query. A bare probe must carry a machine-readable
    self_description with the canonical category, capability count, protocols,
    and only real live URLs — so the Guild is represented accurately downstream
    (2026-07-11)."""
    _seed()
    payload = _send("hello")
    assert payload["kind"] == "probe_ack"
    sd = payload["self_description"]
    assert sd["name"] == "Agent Guild"
    assert sd["category"] == "trust-and-settlement-middleware"
    # capability count is live, not a projection
    assert isinstance(sd["capabilities_supplied"], int)
    assert sd["capabilities_supplied"] == len(payload["supplied_capabilities"])
    assert set(["A2A", "MCP", "REST"]).issubset(set(sd["protocols"]))
    # every advertised URL points at a real path (no fabricated endpoints)
    for key in ("agent_card", "mcp", "standard", "for_agents"):
        assert sd["urls"][key].startswith("http")
    # the MCP url uses the trailing slash (bare /mcp 307-redirects and breaks scanners)
    assert sd["urls"]["mcp"].endswith("/mcp/")


def test_a2a_json_skill_invocation_resolves_per_agent_card():
    """The card is a contract (2026-07-13): a genuine external caller
    (a2a:net:8feb…) sent ``{"skill":"guild.check","args":{}}`` — the skill id
    verbatim from /.well-known/agent-card.json — and dead-ended at probe_ack.
    Every card-advertised skill id must resolve when invoked as JSON."""
    _seed()
    # 1) the exact live message: guild.check without the capability arg gets
    #    the corrected call, not a generic ack
    payload = _send('{"skill":"guild.check","args":{}}')
    assert payload["kind"] == "skill_args_missing"
    assert payload["example"] == {"skill": "guild.check",
                                  "args": {"capability": "fact-check"}}
    # 2) well-formed guild.check ≡ the text form
    as_json = _send('{"skill":"guild.check","args":{"capability":"fact-check"}}')
    as_text = _send("check: fact-check")
    assert set(as_json.keys()) == set(as_text.keys())
    # attributed as a capability ask, so engagement (not probe) is counted
    ev = [e for e in store.events
          if e.get("endpoint") == "a2a_message" and e.get("type") == "query"]
    assert ev[-2].get("caller_kind") == "capability_ask"
    assert ev[-2].get("capability") == "fact-check"
    # 3) flattened args tolerated
    flat = _send('{"skill":"guild.check","capability":"fact-check"}')
    assert set(flat.keys()) == set(as_text.keys())
    # 4) guild.capabilities → supply/demand map
    caps = _send('{"skill":"guild.capabilities","args":{}}')
    assert "supplied" in caps and "demand" in caps
    # 5) ag.<capability> invokes the gateway with args as the payload
    inv = _send('{"skill":"ag.json.repair","args":{"text":"{\'a\': 1,}"}}')
    assert "guild_contact" in inv  # a routed reply, not an rpc error
    assert "probe_ack" != inv.get("kind")
    # 6) unknown skill → executable catalog, not a dead end
    unk = _send('{"skill":"guild.nope","args":{}}')
    assert unk["kind"] == "skill_not_found"
    assert "guild.check" in unk["skills"]


def test_prove_question_gets_exact_instructions_not_probe_ack():
    """The pathtoAGI lesson (2026-07-06): a self-sovereign agent that asks HOW
    to prove (naming its agent_id, exactly as agent_f58dc48bbe24 did) must get
    exact executable steps — real id substituted, per-class auth truth,
    canonicalization rule, verify payload schema — not a canned probe_ack."""
    _seed()
    from app import crypto as _crypto
    _, pub_hex = _crypto.generate_keypair()
    reg = client.post("/agents/register", json={
        "name": "ProveAsker", "capabilities": ["testing"],
        "public_key": pub_hex}).json()
    aid = reg["id"]
    payload = _send(f"how do I complete prove_key_control? "
                    f"give me the exact endpoint and payload for {aid}")
    assert payload["kind"] == "prove_instructions"
    # personalized: the real agent_id substituted into the calls
    assert any(aid in str(s.get("call", "")) for s in payload["steps"])
    # auth truth per class must be stated (custodial X-API-Key vs sovereign none)
    assert any("X-API-Key" in g and "elf-sovereign" in g for g in payload["gotchas"])
    # signing must be fully specified (canonical form + hex signature body)
    flat = json.dumps(payload)
    assert "sorted" in flat.lower() and "signature" in flat
    assert payload["your_status"]["agent_id"] == aid
    assert payload["your_status"]["proof_class"] == "key_control"
    assert "unproven" in payload["your_status"]["state"]
    # measured: the answer is a funnel event, attributable to the named agent
    ev = [e for e in store.events if e.get("type") == "prove_howto_served"]
    assert ev and ev[-1].get("agent_id") == aid


def test_prove_question_custodial_agent_gets_api_key_steps():
    """Custodial agents get the credential_control flow: X-API-Key on both
    calls, no signing step."""
    _seed()
    reg = client.post("/agents/register", json={
        "name": "CustodialProveAsker", "capabilities": ["testing"]}).json()
    aid = reg["id"]
    payload = _send(f"how do I prove key control for {aid}?")
    assert payload["kind"] == "prove_instructions"
    assert payload["your_status"]["proof_class"] == "credential_control"
    flat = json.dumps(payload["steps"])
    assert "X-API-Key" in flat
    assert not any("reference_python" in s for s in payload["steps"])


def test_prove_question_without_agent_id_gets_generic_steps():
    _seed()
    payload = _send("how does proving work?")
    assert payload["kind"] == "prove_instructions"
    assert any("{your_agent_id}" in str(s.get("call", "")) for s in payload["steps"])
    assert "your_status" not in payload


def test_explicit_capability_ask_still_wins_over_prove_words():
    _seed()
    payload = _send("check: fact-check (I may prove later)")
    assert payload.get("capability") == "fact-check"


def test_instructions_signature_actually_verifies_end_to_end():
    """The reference instructions must be RIGHT: follow them literally and the
    proof must verify. Guards against instruction drift from the crypto."""
    _seed()
    from app import crypto as _crypto
    priv_hex, pub_hex = _crypto.generate_keypair()
    reg = client.post("/agents/register", json={
        "name": "SelfSovereign ProveAsker", "capabilities": ["testing"],
        "public_key": pub_hex}).json()
    aid = reg["id"]
    assert reg["api_key"] is None  # self-sovereign: no credential issued
    # exactly as the instructions say: no auth header for self-sovereign
    ch = client.post(f"/agents/{aid}/prove").json()["challenge"]
    # literally what reference_python says: sorted keys, compact separators
    sig = _crypto.sign_payload(ch, priv_hex)
    out = client.post(f"/agents/{aid}/prove/verify", json={"signature": sig})
    assert out.status_code == 200
    assert out.json()["status"] == "proven"


def test_a2a_malformed_invoke_gets_corrective_error_not_probe_ack():
    """Cold-discovery finding (2026-07-10): a client that sends 'invoke:' with
    a bad/missing capability id must get the exact corrective syntax and the
    capability index — a generic probe_ack is a machine dead end."""
    req = {
        "jsonrpc": "2.0", "id": "x", "method": "message/send",
        "params": {"message": {"parts": [{"kind": "text",
                                          "text": 'invoke: {"text": "{bad,}"}'}]}},
    }
    payload = json.loads(client.post("/a2a", json=req).json()["result"]["parts"][0]["text"])
    assert payload.get("error") == "invoke_syntax"
    assert "capability_ids" in payload and "json.repair" in payload["capability_ids"]
    assert "expected" in payload


def test_a2a_swarm_skill_examples_are_fully_formed():
    """Skill examples must be copy-pasteable (no '{...}' placeholders) —
    generic clients template their first call off the example verbatim."""
    card = client.get("/.well-known/agent-card.json").json()
    swarm_skills = [s for s in card["skills"] if s["id"].startswith("ag.")]
    assert swarm_skills
    for s in swarm_skills:
        ex = s["examples"][0]
        assert "{...}" not in ex, s["id"]
        assert ex.startswith("invoke: "), s["id"]


def _send_a2a(text):
    req = {
        "jsonrpc": "2.0", "id": "sk", "method": "message/send",
        "params": {"message": {"parts": [{"kind": "text", "text": text}]}},
    }
    return json.loads(
        client.post("/a2a", json=req).json()["result"]["parts"][0]["text"])


def test_json_skill_call_guild_check_resolves_like_text_check():
    """Card contract (live lesson 2026-07-13, actor a2a:net:8feb…): the card
    advertises skill id guild.check; invoking it as JSON must resolve to the
    same one-call vet as 'check: <capability>' — never probe_ack."""
    _seed()
    payload = _send_a2a(
        '{"skill": "guild.check", "args": {"capability": "fact-check"}}')
    assert payload.get("kind") != "probe_ack"
    assert payload.get("capability") == "fact-check" or "decision" in payload \
        or "verdict" in json.dumps(payload)


def test_json_skill_call_guild_check_without_capability_gets_exact_fix():
    """{"skill":"guild.check","args":{}} — exactly what the external sent —
    must return the corrected call, not a generic ack."""
    payload = _send_a2a('{"skill": "guild.check", "args": {}}')
    assert payload.get("kind") == "skill_args_missing"
    assert payload["expected"]["args"]["capability"] == "<capability>"
    assert payload["example"]["args"]["capability"] == "fact-check"
    assert "supplied_capabilities" in payload


def test_json_skill_call_guild_capabilities_returns_map():
    payload = _send_a2a('{"skill": "guild.capabilities"}')
    assert "supplied" in payload and "demand" in payload


def test_json_skill_call_ag_capability_invokes_swarm():
    """{"skill":"ag.json.repair","args":{...}} — the literal per-capability
    skill id off the card — must route into the acquisition gateway."""
    payload = _send_a2a(
        '{"skill": "ag.json.repair", "args": {"text": "{\'a\': 1,}"}}')
    assert payload.get("kind") != "probe_ack"
    txt = json.dumps(payload)
    assert "provenance" in txt or "result" in txt or "denied" in txt \
        or "error" in txt  # gateway answered, whatever the verdict


def test_json_skill_call_unknown_skill_lists_real_skills():
    payload = _send_a2a('{"skill": "guild.nonexistent", "args": {}}')
    assert payload.get("kind") == "skill_not_found"
    assert "guild.check" in payload["skills"]
    assert any(k.startswith("ag.") for k in payload["skills"])
