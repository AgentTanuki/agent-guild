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
    "description": "Trust agents before delegating work.",
    "version": "2.0.0",
    "repository": {"url": "https://github.com/AgentTanuki/agent-guild",
                   "source": "github"},
    "remotes": [{"type": "streamable-http",
                 "url": "https://agent-guild-5d5r.onrender.com/mcp/"}],
    "websiteUrl": "https://agent-guild-5d5r.onrender.com",
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
    # a legacy pre-payment-enforcement record (1.x) must never certify a
    # 2.0.0 publish
    for legacy in ("1.1.0", "1.2.0"):
        r = rb.verify_readback(_served(version=legacy), EXPECTED)
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


def test_added_remote_or_remote_configuration_mismatches():
    extra = copy.deepcopy(EXPECTED["remotes"])
    extra.append({"type": "streamable-http", "url": "https://evil.invalid"})
    assert rb.verify_readback(_served(remotes=extra), EXPECTED).status == \
        "mismatch"
    configured = copy.deepcopy(EXPECTED["remotes"])
    configured[0]["headers"] = [{"name": "Authorization", "value": "x"}]
    assert rb.verify_readback(_served(remotes=configured), EXPECTED).status == \
        "mismatch"


def test_changed_buyer_facing_description_or_website_mismatches():
    served = _served(
        description="Something else", websiteUrl="https://evil.invalid")
    result = rb.verify_readback(served, EXPECTED)
    assert result.status == "mismatch"
    assert any("description" in reason for reason in result.reasons)
    assert any("websiteUrl" in reason for reason in result.reasons)


def test_stripped_trust_meta_fails_readback():
    served = _served()
    served["server"].pop("_meta")
    r = rb.verify_readback(served, EXPECTED)
    assert r.status == "mismatch"
    assert any("publisher-provided _meta missing" in x for x in r.reasons)


def test_mutated_trust_meta_fails_readback():
    served = _served()
    served["server"]["_meta"][PP]["ai.agent-guild/trust"] = {"contract": "OTHER"}
    r = rb.verify_readback(served, EXPECTED)
    assert r.status == "mismatch"
    assert any("exactly" in x for x in r.reasons)


def test_focused_product_metadata_is_exact_even_without_a_trust_key():
    expected = copy.deepcopy(EXPECTED)
    expected["name"] = "io.github.AgentTanuki/x402-payment-safety"
    expected["_meta"] = {PP: {
        "ai.agent-guild/x402-payment-safety": {
            "tool": "guild_x402_payment_safety",
            "verify": "https://agent-guild.example/verify",
        },
    }}
    served = _served(**copy.deepcopy(expected))
    served["server"]["_meta"][PP][
        "ai.agent-guild/x402-payment-safety"].pop("verify")
    result = rb.verify_readback(served, expected)
    assert result.status == "mismatch"
    assert any("publisher-provided" in reason for reason in result.reasons)


def test_trust_read_product_metadata_is_exact():
    expected = copy.deepcopy(EXPECTED)
    expected["name"] = "io.github.AgentTanuki/agent-guild-trust-reads"
    expected["remotes"] = [{
        "type": "streamable-http",
        "url": "https://agent-guild.example/mcp/trust/",
    }]
    expected["_meta"] = {PP: {
        "ai.agent-guild/trust-reads": {
            "tools": ["guild_preflight", "guild_verify"],
            "accepted_inputs": ["public endpoint URLs", "Agent Passport VCs"],
        },
    }}
    served = _served(**copy.deepcopy(expected))
    served["server"]["_meta"][PP]["ai.agent-guild/trust-reads"][
        "tools"].append("guild_register")
    result = rb.verify_readback(served, expected)
    assert result.status == "mismatch"
    assert any("publisher-provided" in reason for reason in result.reasons)


def test_expected_without_trust_meta_does_not_gate_on_it():
    # if the local server.json carries no trust block, readback must not
    # invent a requirement the registry cannot satisfy
    exp = copy.deepcopy(EXPECTED)
    exp.pop("_meta")
    served = _served()
    served["server"].pop("_meta")
    assert rb.verify_readback(served, exp).ok


def test_search_parser_requires_the_exact_expected_name():
    body = {"servers": [
        _served()["server"],
        {"server": {
            "name": "io.github.AgentTanuki/x402-payment-safety-copy",
            "version": "2.0.0",
        }},
    ]}
    assert rb.search_contains(body, EXPECTED["name"]) is True
    assert rb.search_contains(
        body, "io.github.AgentTanuki/x402-payment-safety") is False
    assert rb.search_contains({}, EXPECTED["name"]) is False
