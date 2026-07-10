"""Machine menu continuation (2026-07-10).

Live telemetry: an external LLM-driven client replied "user: 1" x9 and
"user: 3" to Guild A2A responses and dead-ended at probe_ack. The Guild is
stateless and never issues numbered menus, so the only honest resolution of a
bare option reply is a machine-readable clarification carrying the exact
self-contained actions — never a generic ack, never a guessed mapping.
"""
import os, tempfile

os.environ.setdefault("GUILD_DATA", os.path.join(tempfile.mkdtemp(), "g.json"))

import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def _send(text):
    r = client.post("/a2a", json={
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {"parts": [{"kind": "text", "text": text}]}}})
    return json.loads(r.json()["result"]["parts"][0]["text"])


def _assert_clarification(payload, received):
    assert payload["kind"] == "option_reply_without_context", payload.get("kind")
    assert payload["error"] == "no_session_context"
    assert payload["received"] == received
    actions = {a["action"] for a in payload["actions"]}
    assert {"capabilities.map", "trust.check", "capability.invoke",
            "register"} <= actions
    # every offered action must be self-contained (a full message or full call)
    for a in payload["actions"]:
        assert a.get("send") or a.get("http"), a


def test_actor_style_reply_1():
    _assert_clarification(_send("user: 1"), "user: 1")


def test_actor_style_reply_3():
    _assert_clarification(_send("user: 3"), "user: 3")


def test_bare_digit_and_variants():
    for t in ("1", "2.", "(3)", "option 4", "b"):
        _assert_clarification(_send(t), t)


def test_stale_or_foreign_or_replayed_option_all_get_clarification():
    """There are no continuation tokens, so a stale option, an option minted
    in another session, and a replayed option are all the same case: no
    session context -> clarification, never a guessed action, never an ack."""
    for t in ("user: 1", "user: 1", "99"):   # replay twice + invalid number
        p = _send(t)
        assert p["error"] == "no_session_context"


def test_capabilities_word_still_routes_to_map():
    p = _send("capabilities")
    assert "supplied" in p and "demand" in p


def test_check_and_invoke_unaffected():
    p = _send("check: fact-check")
    assert p.get("capability") == "fact-check"
    p = _send('invoke: json.repair {"text": "{\'a\': 1,}"}')
    assert p.get("capability") == "json.repair" and "provenance" in p


def test_option_reply_never_gets_probe_ack():
    assert _send("user: 1").get("kind") != "probe_ack"
