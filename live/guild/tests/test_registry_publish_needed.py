"""Exact-version MCP Registry recovery decision tests."""
from __future__ import annotations

import copy
import importlib.util
import pathlib
import sys

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location(
    "registry_publish_needed", _SCRIPTS / "registry_publish_needed.py")
needed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(needed)

PP = needed.registry_readback.PUBLISHER_PROVIDED
EXPECTED = {
    "name": "io.github.AgentTanuki/agent-guild",
    "version": "2.1.1",
    "repository": {
        "url": "https://github.com/AgentTanuki/agent-guild",
        "source": "github",
    },
    "remotes": [{
        "type": "streamable-http",
        "url": "https://agent-guild-5d5r.onrender.com/mcp/",
    }],
    "_meta": {PP: {"ai.agent-guild/trust": {"contract": "AGD-1/1.0"}}},
}


def _served(expected=EXPECTED):
    return {
        "server": copy.deepcopy(expected),
        "_meta": {"io.modelcontextprotocol.registry/official": {
            "status": "active", "isLatest": True,
        }},
    }


def test_missing_exact_version_requires_publication():
    out = needed.decide(
        {"status": 404, "title": "Not Found", "detail": "not found"},
        EXPECTED,
    )
    assert out == {
        "needed": True,
        "state": "version_not_found",
        "name": EXPECTED["name"],
        "version": EXPECTED["version"],
    }


def test_exact_readback_match_is_idempotently_current():
    out = needed.decide(_served(), EXPECTED)
    assert out["needed"] is False
    assert out["state"] == "already_current"


@pytest.mark.parametrize("body", [
    {},
    {"server": {**EXPECTED, "repository": {"url": "https://evil.invalid"}}},
    {"server": {**EXPECTED, "version": "2.1.0"}},
])
def test_malformed_or_same_version_drift_fails_closed(body):
    with pytest.raises(ValueError, match="unsafe"):
        needed.decide(body, EXPECTED)


def test_github_output_is_machine_readable(tmp_path):
    output = tmp_path / "github-output"
    needed._write_github_output(output, {
        "needed": True,
        "state": "version_not_found",
        "version": "2.1.1",
    })
    assert output.read_text().splitlines() == [
        "needed=true",
        "state=version_not_found",
        "version=2.1.1",
    ]


def test_multiple_registry_products_dispatch_when_either_is_missing():
    current = needed.decide(_served(), EXPECTED)
    focused = copy.deepcopy(EXPECTED)
    focused["name"] = "io.github.AgentTanuki/x402-payment-safety"
    missing = needed.decide({"status": 404}, focused)
    trust_reads = copy.deepcopy(EXPECTED)
    trust_reads["name"] = "io.github.AgentTanuki/agent-guild-trust-reads"
    trust_current = needed.decide(_served(trust_reads), trust_reads)
    combined = needed.combine_decisions([current, missing, trust_current])
    assert combined["needed"] is True
    assert combined["state"] == "publication_needed"
    assert combined["version"] == EXPECTED["version"]
    assert combined["name"] == [
        EXPECTED["name"], focused["name"], trust_reads["name"]]
