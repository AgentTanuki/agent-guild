"""LIVE, read-only smoke checks for the scout's registry adapters.

Fixture-only tests once concealed a dead A2A URL and two wrong protocol
assumptions. These tests hit the REAL registries — official MCP Registry,
a2aregistry.org, CDP Bazaar — through the scout's own SSRF-safe bounded
fetch, and assert the exact response-shape assumptions the adapters encode.

Read-only: list/search endpoints only; nothing is registered, invoked,
contacted or paid. Gated behind GUILD_LIVE_SMOKE=1 so offline CI stays
deterministic; the acceptance pipeline runs them explicitly.
"""
import json
import os

import pytest

from app.swarm import scout

pytestmark = pytest.mark.skipif(
    os.environ.get("GUILD_LIVE_SMOKE") != "1",
    reason="live network smoke checks run only with GUILD_LIVE_SMOKE=1")


def test_live_a2a_registry_shape_and_adapter_path():
    doc, reason = scout.safe_fetch_json("https://a2aregistry.org/api/agents",
                                        max_bytes=scout.MAX_REGISTRY_BYTES)
    assert reason == "ok", f"live A2A registry fetch failed: {reason}"
    assert isinstance(doc, dict) and isinstance(doc.get("agents"), list), (
        "a2aregistry.org/api/agents must serve {'agents': [...]}")
    assert doc["agents"], "live registry unexpectedly empty"
    a = doc["agents"][0]
    assert "name" in a and ("url" in a or "endpoint" in a)


def test_live_mcp_registry_search_shape():
    url = ("https://registry.modelcontextprotocol.io/v0.1/servers"
           "?search=legal&limit=5")
    doc, reason = scout.safe_fetch_json(url,
                                        max_bytes=scout.MAX_REGISTRY_BYTES)
    if reason != "ok":                      # live registry: one bounded retry
        doc, reason = scout.safe_fetch_json(
            url, max_bytes=scout.MAX_REGISTRY_BYTES)
    assert reason == "ok", f"live MCP Registry fetch failed: {reason}"
    servers = (doc or {}).get("servers")
    assert isinstance(servers, list) and servers, (
        "official MCP Registry must serve {'servers': [...]}")
    row = servers[0]
    srv = row.get("server") if isinstance(row.get("server"), dict) else row
    assert srv.get("name")


def test_live_bazaar_discovery_shape():
    url = ("https://api.cdp.coinbase.com/platform/v2/x402"
           "/discovery/resources?limit=5")
    doc, reason = scout.safe_fetch_json(url,
                                        max_bytes=scout.MAX_REGISTRY_BYTES)
    assert reason == "ok", f"live Bazaar fetch failed: {reason}"
    items = (doc or {}).get("items")
    assert isinstance(items, list) and items, (
        "CDP Bazaar must serve {'items': [...]}")
    it = items[0]
    assert it.get("resource", "").startswith("http")
    assert isinstance(it.get("accepts"), list) and it["accepts"], (
        "every Bazaar item carries x402 `accepts` payment requirements")


def test_live_adapters_yield_at_least_one_valid_candidate_path():
    """Where public supply exists for a broad term, at least one adapter must
    produce a policy-clean candidate — proof the URL + parse + qualify path
    works end to end against production registries."""
    found = []
    for name, adapter in scout.ADAPTERS.items():
        if name == "erc8004":
            continue                     # honestly unsupported
        try:
            found += adapter("search", scout.safe_fetch_json)
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"live adapter {name} crashed: {e}")
    assert found, "no adapter produced a candidate for a broad term"
    from app import reachability
    ok_policy = [c for c in found if c.get("endpoint")
                 and reachability.url_policy_check(c["endpoint"])[0]]
    assert ok_policy, (
        "no candidate passed URL policy — parse or shape assumptions broken: "
        + json.dumps(found[:3])[:500])


def _qualified(rec):
    """A candidate may be CLAIMED qualified only when its recorded fields
    actually meet the definition: valid discovery evidence AND an
    AG-INDEPENDENT protocol-appropriate answer from its infrastructure."""
    ev = rec.get("evidence") or {}
    return bool((ev.get("ag_verified") or {}).get("card_valid")
                and ev.get("independently_reachable"))


def test_live_x402_catalogue_term_yields_candidates_and_a_parseable_402():
    """A term with real Bazaar supply must yield candidates, and at least
    one policy-clean priced resource must answer with a PARSEABLE 402
    challenge — the adapter's whole protocol assumption, exercised live."""
    from app import reachability
    cands = []
    for term in ("email", "search", "data", "api"):
        cands = scout.adapter_x402_bazaar(term, scout.safe_fetch_json)
        if cands:
            break
    assert cands, "no Bazaar candidates for any broad term — catalogue " \
                  "shape or URL assumptions broken"
    verified = []
    tried = []
    for c in cands[:5]:
        if not (c.get("endpoint")
                and reachability.url_policy_check(c["endpoint"])[0]):
            continue
        rec = scout.qualify_candidate(dict(c, capability="smoke"),
                                      scout.safe_fetch_json)
        tried.append({"endpoint": rec.get("endpoint"),
                      "probe": rec["checks"].get("protocol_probe")})
        if rec["evidence"]["protocol_verified"]:
            verified.append(rec)
            assert _qualified(rec), (
                "protocol-verified candidate does not meet the qualified "
                "definition: " + json.dumps(rec["evidence"])[:300])
            break
    assert verified, (
        "no live priced resource presented a parseable 402 challenge — "
        f"probes: {json.dumps(tried)[:500]}")


def test_live_a2a_card_plus_labelled_registry_attestation():
    """A live A2A candidate must be qualified via its agent CARD (the
    side-effect-free discovery step) — never by GETting or messaging its
    execution endpoint — and any registry-asserted health must sit under
    the clearly labelled registry_attested class."""
    from app import reachability
    cands = scout.adapter_a2a_registry("agent", scout.safe_fetch_json)
    assert cands, "live A2A registry yielded no candidates for 'agent'"
    qualified = None
    for c in cands[:5]:
        if not (c.get("endpoint")
                and reachability.url_policy_check(c["endpoint"])[0]):
            continue
        rec = scout.qualify_candidate(dict(c, capability="smoke"),
                                      scout.safe_fetch_json)
        ev = rec["evidence"]
        assert "registry_attested" in ev
        assert "attested by the registry" in ev["registry_attested_note"]
        if _qualified(rec):
            qualified = rec
            assert ev["protocol_verified"] is True    # valid card handshake
            break
    assert qualified, "no live A2A candidate produced a valid card " \
                      "handshake (AG-independent evidence)"


def test_live_mcp_manifest_plus_initialize_or_labelled_auth_gate():
    """A live MCP candidate must carry the registry manifest as evidence and
    answer the bounded Streamable HTTP initialize probe — either with an
    initialize result (protocol verified) or an explicitly labelled
    auth-gated reachability. Never a GET-a-card assumption."""
    from app import reachability
    cands = []
    for term in ("search", "weather", "data"):
        cands = scout.adapter_mcp_registry(term, scout.safe_fetch_json)
        if cands:
            break
    assert cands, "live MCP Registry yielded no candidates"
    evidence_seen = []
    acceptable = None
    for c in cands[:5]:
        if not (c.get("endpoint")
                and reachability.url_policy_check(c["endpoint"])[0]):
            continue
        rec = scout.qualify_candidate(dict(c, capability="smoke"),
                                      scout.safe_fetch_json)
        assert rec["checks"].get("evidence") == "registry_manifest"
        ev = rec["evidence"]
        probe = rec["checks"].get("protocol_probe", "")
        evidence_seen.append({"endpoint": rec.get("endpoint"),
                              "probe": probe})
        if ev["protocol_verified"]:
            acceptable = ("initialize_result", rec)
            assert _qualified(rec)
            break
        if ev["independently_reachable"] and "auth" in probe:
            acceptable = ("auth_gated_labelled", rec)
            break
    assert acceptable, (
        "no live MCP endpoint produced an initialize result or a labelled "
        f"auth-gated answer — probes: {json.dumps(evidence_seen)[:500]}")
