"""guild_inbox delivery surfaces (corrective pass 2026-07-22).

The shipped claim said messages ride "on every authenticated surface — HTTP,
MCP and A2A alike"; what shipped was guild_next (a subset of HTTP) plus the
inbox read. These tests pin the CORRECTED contract (app/inbox.py docstring):

  * MCP — EVERY tool call whose credential authenticates a subject agent
    delivers pending messages (wired through _serve_paid for the paid reads
    and per-tool for the write tools), never on errors, never without auth.
  * A2A — message/send carrying the agent's X-API-Key delivers; anonymous
    messages never do.
  * HTTP — guild_next responses (pre-existing), the journey self-read, and
    the inbox read itself.
  * Privacy — a wrong key, a third-party key, or no key yields NO inbox.

Plus the load-bearing scenario: an agent in AgentServices' exact position
(custodial, organically proved, liveness expiring inside the warn window,
holding ONLY its original api_key) retrieves its liveness-expiry warning —
with the exact re-prove calls — on each surface, with no operator involved
and no credential change.
"""
import asyncio
import json
import os

os.environ.setdefault("GUILD_DATA", "")

from datetime import datetime, timedelta, timezone  # noqa: E402

import mcp.types as mt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from fastmcp import Client  # noqa: E402

from app.main import app  # noqa: E402
from app.mcp_server import mcp as guild_mcp  # noqa: E402
from app.state import store  # noqa: E402
from app import inbox  # noqa: E402

client = TestClient(app)
CLIENT_INFO = mt.Implementation(name="verify", version="0.0")


def _iso(dt):
    return dt.isoformat()


def _now():
    return datetime.now(timezone.utc)


def _register(name):
    r = client.post("/agents/register",
                    json={"name": name, "capabilities": ["fact-check"]})
    assert r.status_code == 200, r.text
    return r.json()


def _queue(agent_id, topic="ping", body="hello"):
    return inbox.queue_message(store, agent_id, topic=topic, body=body)


def _mcp(tool, args):
    async def run():
        async with Client(guild_mcp, client_info=CLIENT_INFO) as c:
            res = await c.call_tool(tool, args)
            sc = res.structured_content
            if isinstance(sc, dict):
                return sc
            return json.loads(res.content[0].text)
    return asyncio.run(run())


def _a2a(text, api_key=None):
    headers = {"X-Api-Key": api_key} if api_key else {}
    r = client.post("/a2a", json={
        "jsonrpc": "2.0", "id": 7, "method": "message/send",
        "params": {"message": {"parts": [{"kind": "text", "text": text}]}},
    }, headers=headers)
    assert r.status_code == 200
    return json.loads(r.json()["result"]["parts"][0]["text"])


def _expiring_proof(days=6):
    return {"verified_at": _iso(_now()),
            "liveness_expires_at": _iso(_now() + timedelta(days=days))}


# --- MCP delivery -----------------------------------------------------------

def test_mcp_paid_read_delivers_inbox_to_authenticated_subject():
    reg = _register("Inbox-Mcp-Check")
    _queue(reg["id"])
    out = _mcp("guild_check", {"capability": "fact-check",
                               "api_key": reg["api_key"]})
    assert "inbox" in out, out
    assert [m["topic"] for m in out["inbox"]["messages"]] == ["ping"]
    assert f"/agents/{reg['id']}/inbox" in out["inbox"]["read_all"]
    # exactly-once in-band delivery: the next call has nothing pending
    again = _mcp("guild_check", {"capability": "fact-check",
                                 "api_key": reg["api_key"]})
    assert "inbox" not in again


def test_mcp_anonymous_or_wrong_key_never_delivers():
    reg = _register("Inbox-Mcp-Private")
    _queue(reg["id"])
    anon = _mcp("guild_check", {"capability": "fact-check"})
    assert "inbox" not in anon
    wrong = _mcp("guild_check", {"capability": "fact-check",
                                 "api_key": "sk_totally_wrong"})
    assert "inbox" not in wrong
    # the message is still pending for the real subject afterwards
    other = _register("Inbox-Mcp-Other")
    theirs = _mcp("guild_check", {"capability": "fact-check",
                                  "api_key": other["api_key"]})
    assert "inbox" not in theirs
    mine = _mcp("guild_check", {"capability": "fact-check",
                                "api_key": reg["api_key"]})
    assert "inbox" in mine


def test_mcp_write_tool_delivers_and_errors_never_do():
    reg = _register("Inbox-Mcp-Prove")
    _queue(reg["id"])
    # error result (wrong key) must NOT carry the inbox
    err = _mcp("guild_prove", {"agent_id": reg["id"], "api_key": "sk_wrong"})
    assert "error" in err and "inbox" not in err
    out = _mcp("guild_prove", {"agent_id": reg["id"],
                               "api_key": reg["api_key"]})
    assert "inbox" in out
    assert [m["topic"] for m in out["inbox"]["messages"]] == ["ping"]


# --- A2A delivery ------------------------------------------------------------

def test_a2a_message_send_delivers_with_api_key_only():
    reg = _register("Inbox-A2A")
    _queue(reg["id"])
    anon = _a2a("capabilities")
    assert "inbox" not in anon
    authed = _a2a("capabilities", api_key=reg["api_key"])
    assert "inbox" in authed, authed
    assert [m["topic"] for m in authed["inbox"]["messages"]] == ["ping"]
    # delivered exactly once in-band
    assert "inbox" not in _a2a("capabilities", api_key=reg["api_key"])


# --- HTTP delivery ------------------------------------------------------------

def test_http_journey_self_read_delivers_third_party_read_does_not():
    reg = _register("Inbox-Journey")
    other = _register("Inbox-Journey-Other")
    _queue(reg["id"])
    third = client.get(f"/agents/{reg['id']}/journey",
                       headers={"X-Api-Key": other["api_key"]})
    assert third.status_code in (200, 402)
    if third.status_code == 200:
        assert "inbox" not in third.json()
    r = client.get(f"/agents/{reg['id']}/journey",
                   headers={"X-Api-Key": reg["api_key"]})
    assert r.status_code == 200
    body = r.json()
    assert "inbox" in body
    assert [m["topic"] for m in body["inbox"]["messages"]] == ["ping"]


# --- the AgentServices scenario ------------------------------------------------

def _agentservices_like(name):
    """An agent in AgentServices' exact position: custodial registration,
    organically completed prove, liveness expiring INSIDE the warn window,
    holding only the api_key it was issued at registration."""
    reg = _register(name)
    agent = store.get_agent(reg["id"])
    agent["proof_of_conduct"] = _expiring_proof(days=6)
    return reg


def _assert_warning(msgs, agent_id):
    warn = [m for m in msgs if m["topic"] == "liveness_expiry"]
    assert warn, msgs
    act = warn[0]["action"]
    assert act["action"] == "refresh_liveness"
    # the exact refresh call, in the same response
    assert f"/agents/{agent_id}/prove" in act["call"]
    assert f"/agents/{agent_id}/prove/verify" in act["call"]


def test_agentservices_gets_warning_on_mcp_with_existing_credential():
    reg = _agentservices_like("AgentServices-Sim-MCP")
    out = _mcp("guild_check", {"capability": "fact-check",
                               "api_key": reg["api_key"]})
    assert "inbox" in out, ("an expiring agent's ONLY interaction may be a "
                            "paid MCP read — the warning must ride on it")
    _assert_warning(out["inbox"]["messages"], reg["id"])


def test_agentservices_gets_warning_on_a2a_with_existing_credential():
    reg = _agentservices_like("AgentServices-Sim-A2A")
    out = _a2a("capabilities", api_key=reg["api_key"])
    assert "inbox" in out
    _assert_warning(out["inbox"]["messages"], reg["id"])


def test_agentservices_gets_warning_on_http_with_existing_credential():
    reg = _agentservices_like("AgentServices-Sim-HTTP")
    r = client.get(f"/agents/{reg['id']}/journey",
                   headers={"X-Api-Key": reg["api_key"]})
    assert r.status_code == 200 and "inbox" in r.json()
    _assert_warning(r.json()["inbox"]["messages"], reg["id"])
    # and the free subject inbox read shows it too (view never consumes)
    r2 = client.get(f"/agents/{reg['id']}/inbox",
                    headers={"X-Api-Key": reg["api_key"]})
    assert r2.status_code == 200
    _assert_warning(r2.json()["messages"], reg["id"])


def test_agentservices_warning_survives_expiry_until_reproved():
    """Even AFTER 07-28-style expiry the warning still rides on the agent's
    next authenticated call — an expired proof re-warns, it never goes
    silent."""
    reg = _register("AgentServices-Sim-Late")
    agent = store.get_agent(reg["id"])
    agent["proof_of_conduct"] = _expiring_proof(days=-2)  # already expired
    out = _mcp("guild_check", {"capability": "fact-check",
                               "api_key": reg["api_key"]})
    assert "inbox" in out
    _assert_warning(out["inbox"]["messages"], reg["id"])
    assert "EXPIRED" in out["inbox"]["messages"][0]["body"]
