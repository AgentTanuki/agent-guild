"""Readback parser tests — the MCP publish workflow's hard gate must be
trustworthy against every response shape the official registry can produce:
successful, legacy (flat / old readback shapes), missing-version and
malformed. Pure-parser tests; no network."""
import copy
import importlib.util
import pathlib
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"
_spec = importlib.util.spec_from_file_location(
    "registry_readback", _SCRIPTS / "registry_readback.py")
rb = importlib.util.module_from_spec(_spec)
sys.modules["registry_readback"] = rb
_spec.loader.exec_module(rb)

PP = rb.PUBLISHER_PROVIDED
TRUST = {"contract": "AGD-1/1.0", "proof_suite": "eddsa-jcs-2022"}

EXPECTED = {
    "name": "io.github.AgentTanuki/agent-guild",
    "version": "1.2.0",
    "repository": {"url": "https://github.com/AgentTanuki/agent-guild",
                   "source": "github"},
    "remotes": [{"type": "streamable-http",
                 "url": "https://agent-guild-5d5r.onrender.com/mcp/"}],
    "_meta": {PP: {"ai.agent-guild/trust": TRUST}},
}


def _served(**overrides):
    srv = copy.deepcopy(EXPECTED)
    srv.update(overrides)
    return {"server": srv,
            "_meta": {"io.modelcontextprotocol.registry/official": {
                "status": "active", "isLatest": True}}}


def test_successful_readback_passes():
    r = rb.verify_readback(_served(), EXPECTED)
    assert r.ok and r.status == "ok" and not r.reasons


def test_missing_version_is_not_found_not_ok():
    body = {"title": "Not Found", "status": 404, "detail": "Server not found"}
    r = rb.verify_readback(body, EXPECTED)
    assert r.status == "not_found" and not r.ok


def test_malformed_response_is_flagged():
    for body in ({}, {"servers": []}, ["nonsense"], {"detail": "teapot"}):
        r = rb.verify_readback(body, EXPECTED)
        assert r.status == "malformed", body
        assert not r.ok


def test_legacy_flat_shape_still_verifies():
    # older readback shapes carried the ServerJSON at the top level
    flat = copy.deepcopy(EXPECTED)
    r = rb.verify_readback(flat, EXPECTED)
    assert r.ok


def test_legacy_listing_of_wrong_version_mismatches():
    # the legacy 1.1.0 record must never certify a 1.2.0 publish
    r = rb.verify_readback(_served(version="1.1.0"), EXPECTED)
    assert r.status == "mismatch"
    assert any("version" in x for x in r.reasons)


def test_case_mismatched_name_is_a_mismatch():
    r = rb.verify_readback(
        _served(name="io.github.agenttanuki/agent-guild"), EXPECTED)
    assert r.status == "mismatch"
    assert any("name" in x for x in r.reasons)


def test_wrong_repository_and_missing_remote_mismatch():
    r = rb.verify_readback(
        _served(repository={"url": "https://github.com/evil/fork",
                            "source": "github"},
                remotes=[{"type": "streamable-http",
                          "url": "https://evil.example/mcp/"}]),
        EXPECTED)
    assert r.status == "mismatch"
    assert any("repository" in x for x in r.reasons)
    assert any("remotes" in x for x in r.reasons)


def test_stripped_trust_meta_fails_readback():
    served = _served()
    served["server"].pop("_meta")
    r = rb.verify_readback(served, EXPECTED)
    assert r.status == "mismatch"
    assert any("trust _meta missing" in x for x in r.reasons)


def test_mutated_trust_meta_fails_readback():
    served = _served()
    served["server"]["_meta"][PP]["ai.agent-guild/trust"] = {"contract": "OTHER"}
    r = rb.verify_readback(served, EXPECTED)
    assert r.status == "mismatch"
    assert any("exactly" in x for x in r.reasons)


def test_expected_without_trust_meta_does_not_gate_on_it():
    # if the local server.json carries no trust block, readback must not
    # invent a requirement the registry cannot satisfy
    exp = copy.deepcopy(EXPECTED)
    exp.pop("_meta")
    served = _served()
    served["server"].pop("_meta")
    assert rb.verify_readback(served, exp).ok
