"""The post-prove credential bundle + journey gating (passport programme
2026-07-23).

One continuous flow: the moment prove/verify succeeds (HTTP and MCP alike),
the response carries the complete passport bundle — fetch URL, offline
verify_call, badge, how to EXPOSE it, and the existing author-first-
attestation guidance as next_evidence_call. The journey ladder steers any
prove_completed agent without a first_passport to `fetch_passport` (no longer
gated on stage ≥ 3). Wart fix: the READ path GET /agents/{id}/journey never
stamps the prove_offered milestone — only guild_next / MCP register do.
"""
import asyncio
import json
import os

os.environ.setdefault("GUILD_DATA", "")

import mcp.types as mt  # noqa: E402
from fastmcp import Client  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.mcp_server import mcp as guild_mcp  # noqa: E402
from app.state import store  # noqa: E402

client = TestClient(app)

CLIENT_INFO = mt.Implementation(name="verify", version="0.0")


def _mcp_call(tool, args):
    async def run():
        async with Client(guild_mcp, client_info=CLIENT_INFO) as c:
            res = await c.call_tool(tool, args)
            return json.loads(res.content[0].text)
    return asyncio.run(run())


def _register(name):
    r = client.post("/agents/register",
                    json={"name": name, "capabilities": ["x"]})
    assert r.status_code == 200
    return r.json()


def _prove(reg):
    h = {"X-API-Key": reg["api_key"]}
    assert client.post(f"/agents/{reg['id']}/prove",
                       headers=h).status_code == 200
    r = client.post(f"/agents/{reg['id']}/prove/verify", headers=h)
    assert r.status_code == 200
    return r.json()


def _assert_bundle(passport, aid):
    assert passport["url"].endswith(f"/agents/{aid}/passport")
    vc = passport["verify_call"]
    assert vc["method"] == "POST" and vc["url"].endswith("/credentials/verify")
    assert "credential" in vc["body"]
    assert passport["badge_url"].endswith(f"/agents/{aid}/badge.svg")
    assert "agent-guild-did.json" in passport["expose"]["how"]
    # the evidence path rides the same response (author-first-attestation)
    assert passport["next_evidence_call"]["action"] == "author_first_attestation"


def test_http_prove_verify_serves_the_passport_bundle():
    reg = _register("BundleHttp")
    out = _prove(reg)
    assert out["status"] == "proven"
    _assert_bundle(out["passport"], reg["id"])
    # the bundle's URL actually serves the credential, free
    path = "/" + out["passport"]["url"].split("/", 3)[3]
    assert client.get(path).status_code == 200


def test_mcp_prove_verify_serves_the_passport_bundle():
    reg = _mcp_call("guild_register",
                    {"name": "BundleMcp", "capabilities": ["x"]})
    _mcp_call("guild_prove", {"agent_id": reg["id"], "api_key": reg["api_key"]})
    out = _mcp_call("guild_prove_verify",
                    {"agent_id": reg["id"], "api_key": reg["api_key"]})
    assert out["status"] == "proven"
    _assert_bundle(out["passport"], reg["id"])


def test_journey_steers_freshly_proved_stage2_agent_to_passport():
    reg = _register("BundleJourney")
    _prove(reg)
    j = client.get(f"/agents/{reg['id']}/journey",
                   headers={"X-API-Key": reg["api_key"]}).json()
    assert j["stage"] == 2                       # freshly proved, NOT standing
    actions = [a["action"] for a in j["next_actions"]]
    assert "fetch_passport" in actions
    # earning first_passport spends the rung
    assert client.get(f"/agents/{reg['id']}/passport").status_code == 200
    j2 = client.get(f"/agents/{reg['id']}/journey",
                    headers={"X-API-Key": reg["api_key"]}).json()
    assert "fetch_passport" not in [a["action"] for a in j2["next_actions"]]


def test_unproven_agent_is_not_steered_to_passport():
    reg = _register("BundleUnproven")
    j = client.get(f"/agents/{reg['id']}/journey",
                   headers={"X-API-Key": reg["api_key"]}).json()
    assert "fetch_passport" not in [a["action"] for a in j["next_actions"]]


def test_journey_read_path_never_stamps_prove_offered():
    """Wart fix: GET /agents/{id}/journey is a READ — serving the ladder to a
    reader is not the rung being offered to the agent, so it must not stamp
    the milestone the proving funnel counts as offered."""
    rec = store.register_agent("WartProbe", ["x"], {})   # no guild_next path
    assert "prove_offered" not in (rec.get("milestones") or {})
    r = client.get(f"/agents/{rec['id']}/journey")
    assert r.status_code == 200
    assert r.json()["next_actions"][0]["action"] == "prove_key_control"
    assert "prove_offered" not in \
        (store.get_agent(rec["id"]).get("milestones") or {})


def test_guild_next_path_still_stamps_prove_offered():
    """The offer IS still counted where it is actually served: the register
    response embeds guild_next, which serves the rung as primary action."""
    reg = _register("WartControl")
    assert "prove_offered" in (store.get_agent(reg["id"]).get("milestones") or {})
