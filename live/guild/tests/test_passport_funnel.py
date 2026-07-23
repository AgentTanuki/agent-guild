"""GET /funnel/passports — the passport acquisition funnel, honestly measured
(passport programme 2026-07-23).

offer_served → offer_followed (register carrying src 'passport_offer:*') →
registered → control_proved → passport_issued → passport_verified →
evidence_attached → returned. Attribution discipline mirrors /funnel and
tests/test_attribution_honesty.py: the headline count is genuine external
only, first-party and unknown are shown separately and NEVER merged into an
external number, and per-surface counts come from the write-time `surface`
field ('unknown_surface' for events predating it).
"""
import os

os.environ.setdefault("GUILD_DATA", "")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.state import store  # noqa: E402

client = TestClient(app)

EXT_UA = "external-agent-framework/1.4 (langgraph)"


def _stage(funnel, name):
    return next(s for s in funnel["stages"] if s["stage"] == name)


def test_funnel_shape_and_stage_order():
    f = client.get("/funnel/passports").json()
    names = [s["stage"] for s in f["stages"]]
    assert names == ["offer_served", "offer_followed", "registered",
                     "control_proved", "passport_issued", "passport_verified",
                     "evidence_attached", "returned"]
    for s in f["stages"]:
        assert set(s) >= {"stage", "count", "total", "breakdown",
                          "by_surface", "distinct_actors", "source"}
        assert set(s["breakdown"]) == {"external", "first_party", "unknown"}
        # the headline is the external number, by construction
        assert s["count"] == s["breakdown"]["external"]
    assert isinstance(f["abandonment"], dict)
    assert "exclusions" in f


def test_register_src_meta_counts_offer_followed():
    before = _stage(store.passport_funnel(), "offer_followed")
    r = client.post("/agents/register",
                    json={"name": "OfferFollower", "capabilities": ["x"],
                          "src": "passport_offer:llms"},
                    headers={"User-Agent": EXT_UA})
    assert r.status_code == 200
    ev = next(e for e in reversed(store.events)
              if e.get("type") == "register"
              and e.get("agent_id") == r.json()["id"])
    assert ev["src"] == "passport_offer:llms"
    after = _stage(store.passport_funnel(), "offer_followed")
    assert after["total"] == before["total"] + 1
    assert after["breakdown"]["external"] == \
        before["breakdown"]["external"] + 1     # framework UA, not ours


def test_register_without_src_never_counts_offer_followed():
    before = _stage(store.passport_funnel(), "offer_followed")
    r = client.post("/agents/register",
                    json={"name": "NoSrc", "capabilities": ["x"]},
                    headers={"User-Agent": EXT_UA})
    assert r.status_code == 200
    after = _stage(store.passport_funnel(), "offer_followed")
    assert after["total"] == before["total"]


def test_src_is_validated():
    r = client.post("/agents/register",
                    json={"name": "BadSrc", "capabilities": ["x"],
                          "src": "NOT VALID SRC!"})
    assert r.status_code == 422
    r = client.post("/agents/register",
                    json={"name": "LongSrc", "capabilities": ["x"],
                          "src": "a" * 65})
    assert r.status_code == 422


def test_first_party_register_with_src_never_reads_external():
    """The sacred rule: a first-party register carrying a passport_offer src
    lands in the first_party breakdown, never in any external count."""
    before = _stage(store.passport_funnel(), "offer_followed")
    r = client.post("/agents/register",
                    json={"name": "FP-Follower", "capabilities": ["x"],
                          "src": "passport_offer:manifest"},
                    headers={"User-Agent": EXT_UA, "X-Guild-Source": "seed"})
    assert r.status_code == 200
    after = _stage(store.passport_funnel(), "offer_followed")
    assert after["total"] == before["total"] + 1
    assert after["breakdown"]["first_party"] == \
        before["breakdown"]["first_party"] + 1
    assert after["breakdown"]["external"] == before["breakdown"]["external"]
    assert after["count"] == after["breakdown"]["external"]


def test_tooling_ua_register_with_src_reads_unknown_never_external():
    before = _stage(store.passport_funnel(), "offer_followed")
    r = client.post("/agents/register",
                    json={"name": "CurlFollower", "capabilities": ["x"],
                          "src": "passport_offer:check"},
                    headers={"User-Agent": "curl/8.5.0"})
    assert r.status_code == 200
    after = _stage(store.passport_funnel(), "offer_followed")
    assert after["breakdown"]["unknown"] == \
        before["breakdown"]["unknown"] + 1
    assert after["breakdown"]["external"] == before["breakdown"]["external"]


def test_full_stage_walk_with_surfaces_and_actors():
    reg = client.post("/agents/register",
                      json={"name": "FunnelWalker", "capabilities": ["x"],
                            "src": "passport_offer:llms"},
                      headers={"User-Agent": EXT_UA}).json()
    key, aid = reg["api_key"], reg["id"]
    h = {"X-API-Key": key, "User-Agent": EXT_UA}
    assert client.post(f"/agents/{aid}/prove", headers=h).status_code == 200
    r = client.post(f"/agents/{aid}/prove/verify", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "proven"
    assert client.get(f"/agents/{aid}/passport").status_code == 200
    f = store.passport_funnel()
    assert _stage(f, "control_proved")["total"] >= 1
    assert _stage(f, "passport_issued")["total"] >= 1
    # registered via HTTP with a real actor key → surface http, real actor
    reg_stage = _stage(f, "registered")
    assert reg_stage["by_surface"].get("http", 0) >= 1
    assert reg_stage["distinct_actors"] >= 1


def test_mcp_register_src_lands_with_mcp_surface():
    import asyncio
    import json as _json

    import mcp.types as mt
    from fastmcp import Client

    from app.mcp_server import mcp as guild_mcp

    async def run():
        async with Client(guild_mcp, client_info=mt.Implementation(
                name="verify", version="0.0")) as c:
            res = await c.call_tool("guild_register", {
                "name": "McpFollower", "capabilities": ["x"],
                "src": "passport_offer:mcp"})
            return _json.loads(res.content[0].text)

    reg = asyncio.run(run())
    ev = next(e for e in reversed(store.events)
              if e.get("type") == "register" and e.get("agent_id") == reg["id"])
    assert ev["src"] == "passport_offer:mcp"
    assert ev["surface"] == "mcp"


def test_abandonment_reasons_ride_the_funnel():
    reg = client.post("/agents/register",
                      json={"name": "FunnelQuitter", "capabilities": ["x"]}).json()
    r = client.post("/feedback/abandonment",
                    json={"stage": "prove", "reason_code": "proof_too_hard"},
                    headers={"X-API-Key": reg["api_key"]})
    assert r.status_code == 200
    f = client.get("/funnel/passports").json()
    assert f["abandonment"].get("proof_too_hard", 0) >= 1
