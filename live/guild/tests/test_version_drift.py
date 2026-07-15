"""Machine-visible release identity: every surface a machine reads must
report the SAME version, and a breaking payment-enforcement change must never
silently reuse a version that was already published.

Context (A3, 2026-07-15): MCP/A2A payment enforcement (previously-free
guild_check & co now answer payment challenges) shipped while 1.2.0 was
already live on the MCP Registry. For machines that is a breaking contract
change → deliberate MAJOR bump to 2.0.0, with the x402 payment mechanism and
priced operations declared in the publisher-provided registry metadata.
"""
import asyncio
import json
import pathlib

from fastapi.testclient import TestClient

from app import __version__
from app.billing import PRICING

REPO = pathlib.Path(__file__).resolve().parents[3]
GUILD = pathlib.Path(__file__).resolve().parents[1]

# every version that has EVER been published to the MCP Registry — all of
# them describe the pre-payment-enforcement contract and may never be reused.
PUBLISHED_PRE_ENFORCEMENT_VERSIONS = {"1.0.0", "1.1.0", "1.2.0"}


def test_breaking_payment_enforcement_never_reuses_a_published_version():
    assert __version__ not in PUBLISHED_PRE_ENFORCEMENT_VERSIONS, (
        "paid MCP/A2A behaviour changed after this version was published — "
        "bump the version (semver MAJOR for machine-breaking changes)")
    major = int(__version__.split(".")[0])
    assert major >= 2, (
        "payment enforcement on previously-free MCP/A2A operations is a "
        "breaking change for machine consumers; it requires a MAJOR bump")


def test_every_machine_surface_reports_the_same_version():
    from app.main import app
    from app.mcp_server import mcp
    from app.a2a import _agent_card
    with TestClient(app) as client:
        assert client.get("/release").json()["version"] == __version__
        assert client.get("/openapi.json").json()["info"]["version"] == \
            __version__
        manifest = client.get("/.well-known/agent-guild.json").json()
        assert manifest["version"] == __version__
        card = client.get("/.well-known/agent-card.json").json()
        assert card["version"] == __version__
    assert _agent_card("https://x.example")["version"] == __version__
    # FastMCP serverInfo
    assert mcp.version == __version__
    # server.json + contract.json (committed, generated artifacts)
    server = json.loads((REPO / "server.json").read_text())
    assert server["version"] == __version__
    contract = json.loads((GUILD / "contract" / "contract.json").read_text())
    assert contract["service"]["version"] == __version__


def test_registry_metadata_declares_x402_payments_and_pricing():
    server = json.loads((REPO / "server.json").read_text())
    pp = server["_meta"][
        "io.modelcontextprotocol.registry/publisher-provided"]
    pay = pp["ai.agent-guild/payments"]
    assert pay["mechanism"] == "x402"
    assert pay["x402_version"] == 2
    assert "guild_check" in pay["priced_mcp_tools"]
    # pricing in the registry listing matches the actual billing table
    assert pay["pricing_credits"] == {op: cost
                                      for op, cost in PRICING.items()}
    # the whole publisher-provided blob stays under the registry's 4KB cap
    assert len(json.dumps(pp).encode()) < 4096


def test_contract_payments_block_matches_billing_and_gateway():
    contract = json.loads((GUILD / "contract" / "contract.json").read_text())
    pay = contract["payments"]
    assert pay["mechanism"] == "x402" and pay["x402_version"] == 2
    assert set(pay["priced_operations"]) == set(PRICING)
    for op, row in pay["priced_operations"].items():
        assert row["credits"] == PRICING[op]
        assert row["usdc_atomic"] == PRICING[op] * 1000
    # the priced MCP tools declared in the contract exist in the MCP tool list
    assert set(pay["priced_mcp_tools"]) <= set(contract["mcp_tools"])
    assert "guild.check" in pay["priced_a2a_skills"]


def test_generated_artifacts_have_zero_drift_from_the_generator():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "contract_generate", GUILD / "contract" / "generate.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    contract = gen.build_contract()
    committed = json.loads((GUILD / "contract" / "contract.json").read_text())
    assert committed == json.loads(
        json.dumps(contract)), "contract.json drifted — run `make contract`"
    server = json.loads((REPO / "server.json").read_text())
    assert server == json.loads(json.dumps(gen.derived_server_json(contract)))
