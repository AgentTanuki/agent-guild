"""Discovery swarm — identity factory: signed documents, publish gate, index,
A2A card + MCP exposure, manifest wiring."""
import os
os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, store  # noqa: E402
from app.crypto import verify_jcs  # noqa: E402
from app.swarm.capabilities import CAPABILITIES  # noqa: E402
from app.swarm.identity import IdentityRegistry, registry, build_identity  # noqa: E402
from app.swarm.router import ensure_built  # noqa: E402

client = TestClient(app)


def setup_module():
    ensure_built()


def test_index_lists_all_published_identities():
    r = client.get("/.well-known/ag-identities/index.json")
    assert r.status_code == 200
    idx = r.json()
    assert idx["count"] == len(CAPABILITIES)
    entry = idx["identities"][0]
    for key in ("ag_id", "capability", "version", "invoke", "mcp_tool",
                "document", "health"):
        assert key in entry
    assert idx["terms"].endswith("/terms.json")


def test_identity_document_is_complete_and_signed():
    idx = client.get("/.well-known/ag-identities/index.json").json()
    doc = client.get(f"/identities/{idx['identities'][0]['ag_id']}").json()
    ident = doc["identity"]
    # required fields from the brief
    for key in ("ag_id", "name", "capability", "protocols", "auth", "pricing",
                "expected_latency_ms", "reliability", "benchmark",
                "context_limits", "known_failure_modes", "prohibited_uses",
                "owner", "guild_membership", "created_at", "updated_at", "health"):
        assert key in ident, key
    assert ident["capability"]["input_schema"]["type"] == "object"
    assert ident["capability"]["version"]
    assert ident["benchmark"]["ok"] is True
    assert ident["health"] == "passing"
    # signature verifies against the Guild key
    sig = doc["signature"]
    assert verify_jcs(ident, sig["signature"], sig["public_key"])
    assert sig["signer_did"] == store.guild_did()


def test_publish_gate_excludes_failing_capability():
    # a capability whose fixture suite fails must NOT get an identity
    import app.swarm.capabilities as caps_mod
    broken = caps_mod.Capability(
        id="test.broken", version="1.0.0", name="Broken", summary="s",
        description="d", tags=("t",),
        input_schema={"type": "object", "properties": {}, "required": [],
                      "additionalProperties": False},
        output_schema={"type": "object"},
        run=lambda p: {"x": 1},
        fixtures=({"input": {}, "expect_subset": {"x": 2}},),  # will fail
        failure_modes=("f",), prohibited_uses=("p",),
        demand_hypothesis="h")
    caps_mod.CAPABILITIES["test.broken"] = broken
    try:
        reg = IdentityRegistry()
        result = reg.build("http://test", store.guild_identity(), {})
        assert "test.broken" in result["excluded"]
        assert reg.for_capability("test.broken") is None
        assert result["published"] == len(caps_mod.CAPABILITIES) - 1
    finally:
        del caps_mod.CAPABILITIES["test.broken"]


def test_unknown_identity_404():
    assert client.get("/identities/agid_nope").status_code == 404


def test_swarm_identities_registered_as_first_party_supply():
    swarm_agents = [a for a in store.agents.values()
                    if (a.get("metadata") or {}).get("swarm_identity")]
    assert len(swarm_agents) == len(CAPABILITIES)
    assert all(a["first_party"] for a in swarm_agents)   # excluded from growth
    # idempotent: ensure_built again doesn't duplicate
    registry._built_at = None
    ensure_built()
    again = [a for a in store.agents.values()
             if (a.get("metadata") or {}).get("swarm_identity")]
    assert len(again) == len(CAPABILITIES)


def test_a2a_card_advertises_swarm_skills():
    card = client.get("/.well-known/agent-card.json").json()
    ids = {s["id"] for s in card["skills"]}
    assert "guild.invoke" in ids
    assert "ag.json.repair" in ids
    assert len(ids) >= len(CAPABILITIES) + 2


def test_manifest_links_swarm_surfaces():
    m = client.get("/.well-known/agent-guild.json").json()
    assert "invocable_capabilities" in m
    assert m["invocable_capabilities"]["index"] == "/.well-known/ag-identities/index.json"
    assert m["discovery"]["ag_identities"] == "/.well-known/ag-identities/index.json"


def test_llms_txt_mentions_guest_invocation():
    txt = client.get("/llms.txt").text
    assert "/invoke/" in txt and "ag-identities" in txt


def test_mcp_tools_registered_per_capability():
    import asyncio
    from app.mcp_server import mcp
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert "ag_capabilities" in names
    for cap_id in CAPABILITIES:
        assert "ag_" + cap_id.replace(".", "_") in names
