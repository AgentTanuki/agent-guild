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
