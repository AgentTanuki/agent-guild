"""The Registry gate validates only—and all of—the published MCP surfaces."""
from __future__ import annotations

import importlib.util
import pathlib


REPO = pathlib.Path(__file__).resolve().parents[3]
GATE_PATH = REPO / "live" / "scripts" / "registry_live_gate.py"
SPEC = importlib.util.spec_from_file_location("registry_live_gate_test", GATE_PATH)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def _card(tools, *, endpoint="/mcp/", version=None):
    return {
        "serverInfo": {"version": version or gate.CONTRACT["service"]["version"]},
        "transport": {"endpoint": endpoint},
        "tools": [{"name": name} for name in tools],
    }


def test_card_validation_accepts_exact_contract():
    assert gate.card_failures(
        _card(gate.CONTRACT["mcp_tools"]),
        expected_tools=gate.CONTRACT["mcp_tools"], endpoint="/mcp/") == []


def test_card_validation_fails_tool_version_and_endpoint_drift():
    failures = gate.card_failures(
        _card(["wrong"], endpoint="/wrong/", version="0.0.0"),
        expected_tools=gate.CONTRACT["mcp_tools"], endpoint="/mcp/")
    assert len(failures) == 3
    assert any("tools differ" in failure for failure in failures)
    assert any("version differs" in failure for failure in failures)
    assert any("endpoint differs" in failure for failure in failures)


def test_gate_scope_covers_both_registry_manifests_not_unrelated_rest_routes():
    source = GATE_PATH.read_text()
    assert 'CONTRACT["mcp_tools"]' in source
    assert 'CONTRACT["payment_safety_mcp_tools"]' in source
    assert "/.well-known/mcp/server-card.json" in source
    assert "/.well-known/mcp/payment-safety-server-card.json" in source
    assert 'CONTRACT["rest"]' not in source
