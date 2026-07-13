"""One canonical contract, zero drift: the committed contract/contract.json and
its derived artifacts (server.json, docs/INTERFACE.md) must exactly match what
the running code generates, and the live surfaces (agent card, MCP tool list,
REST routes) must match the contract. Any surface change forces a reviewed
contract diff or CI fails."""
import json
import os
import pathlib

os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parents[1]        # live/guild
REPO = pathlib.Path(__file__).resolve().parents[3]        # repo root

client = TestClient(app)


def _generated():
    import contract.generate as gen
    return gen


def test_committed_contract_matches_code():
    gen = _generated()
    fresh = gen.build_contract()
    committed = json.loads((HERE / "contract" / "contract.json").read_text())
    assert fresh == committed, (
        "contract/contract.json is stale — run `python contract/generate.py` "
        "and commit the diff")


def test_derived_server_json_matches_contract():
    gen = _generated()
    committed_contract = json.loads((HERE / "contract" / "contract.json").read_text())
    expected = gen.derived_server_json(committed_contract)
    actual = json.loads((REPO / "server.json").read_text())
    assert actual == expected, "server.json drifted from the contract — run `make contract`"


def test_derived_interface_doc_matches_contract():
    gen = _generated()
    committed_contract = json.loads((HERE / "contract" / "contract.json").read_text())
    expected = gen.derived_interface_md(committed_contract)
    actual = (REPO / "docs" / "INTERFACE.md").read_text()
    assert actual == expected, "docs/INTERFACE.md drifted — run `make contract`"


def test_live_agent_card_matches_contract_skills():
    committed = json.loads((HERE / "contract" / "contract.json").read_text())
    card = client.get("/.well-known/agent-card.json").json()
    card_skills = {s["id"] for s in card.get("skills", [])}
    expected = set(committed["a2a_skills_static"]) | set(committed["a2a_dynamic_skills"])
    assert expected <= card_skills, (
        f"agent card is missing contract skills: {expected - card_skills}")


def test_rest_contract_paths_are_served():
    committed = json.loads((HERE / "contract" / "contract.json").read_text())
    served = {}
    def walk(routes):
        for r in routes:
            ms = getattr(r, "methods", None)
            if ms:
                served.setdefault(r.path, set()).update(ms - {"HEAD", "OPTIONS"})
            if getattr(r, "routes", None):
                walk(r.routes)
    walk(app.routes)
    for entry in committed["rest"]:
        assert entry["path"] in served, f"contract path not served: {entry['path']}"
        assert set(entry["methods"]) <= served[entry["path"]], (
            f"method drift on {entry['path']}")


def test_mcp_tools_match_contract():
    import asyncio
    from app.mcp_server import mcp
    committed = json.loads((HERE / "contract" / "contract.json").read_text())
    tools = sorted(t.name for t in asyncio.run(mcp.list_tools()))
    assert tools == committed["mcp_tools"]
