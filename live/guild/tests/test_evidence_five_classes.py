"""Five-class discovery evidence semantics (machine-attribution pass).

Defect reproduced: a valid A2A agent card was recorded as proof that the
EXECUTION endpoint works ("protocol_verified") — but a card only proves the
DISCOVERY document exists and parses. The A2A specification offers no
side-effect-free execution operation (message/send creates work), so
execution verification for A2A must honestly stay FALSE.

Classes under test:
  discovery_document_reachable   AG fetched the candidate's own discovery
                                 document (agent card);
  discovery_protocol_verified    that document is valid for the protocol;
  execution_endpoint_reachable   the EXECUTION endpoint answered AG;
  execution_protocol_verified    it answered with its declared protocol via
                                 a genuinely side-effect-free operation;
  registry_attested              what a registry asserts — labelled, never
                                 promoted.

Events: candidate_discovery_verified and candidate_execution_verified are
SEPARATE; candidate_endpoint_verified never fires from card-only evidence.
"""
import uuid

import pytest

from app import demand
from app.state import store
from app.swarm import scout

EXT_UA = "external-agent-framework/2.0 (crewai)"


def _cap():
    return "five-cap-" + uuid.uuid4().hex[:8]


def _card(cap, endpoint):
    return {"name": f"s-{cap}", "url": endpoint,
            "skills": [{"id": cap}], "protocolVersion": "0.3.0"}


def _events(t):
    return sum(1 for e in store.events if e.get("type") == t)


def test_a2a_card_proves_discovery_only_never_execution():
    cap = _cap()
    endpoint = "https://exec.example/a2a"
    card_url = "https://exec.example/.well-known/agent-card.json"
    contacted = []

    def fetch(url, **kw):
        contacted.append(url)
        return (_card(cap, endpoint), "ok") if url == card_url else (
            None, "http_404")

    rec = scout.qualify_candidate(
        {"source": "a2a_registry", "endpoint": endpoint,
         "card_url": card_url, "protocol": "a2a", "capability": cap},
        fetch, probe=None)
    ev = rec["evidence"]
    assert ev["discovery_document_reachable"] is True
    assert ev["discovery_protocol_verified"] is True
    assert ev["execution_endpoint_reachable"] is False, (
        "the execution endpoint was never contacted — a card cannot prove "
        "it is reachable")
    assert ev["execution_protocol_verified"] is False, (
        "A2A offers no side-effect-free execution operation; execution "
        "verification must stay false rather than send message/send")
    assert endpoint not in contacted, "execution endpoint was contacted"


def test_no_message_send_is_ever_used_to_probe():
    import inspect
    src = inspect.getsource(scout)
    assert "message/send" not in src, (
        "the scout must never send message/send merely to probe an agent")


def test_split_events_and_no_endpoint_verified_from_card_only():
    cap = _cap()
    endpoint = f"https://{cap}.example/a2a"
    demand.record_demand(cap, transport="http", actor="ev-" + cap,
                         ua=EXT_UA)

    def fetch(url, **kw):
        if "a2aregistry" in url:
            return ({"agents": [{"name": "s", "url": endpoint,
                                 "description": f"does {cap}"}]}, "ok")
        if url.endswith("agent-card.json") or url.startswith(endpoint):
            return (_card(cap, endpoint), "ok")
        return ({"servers": [], "items": [], "resources": []}, "ok")

    before_disc = _events("candidate_discovery_verified")
    before_exec = _events("candidate_execution_verified")
    before_legacy = _events("candidate_endpoint_verified")
    scout.run_scout(store, fetch=fetch,
                    adapters={"a2a_registry": scout.adapter_a2a_registry})
    assert _events("candidate_discovery_verified") == before_disc + 1, (
        "a verified discovery document must emit its own event")
    assert _events("candidate_execution_verified") == before_exec, (
        "no execution evidence exists — candidate_execution_verified must "
        "not fire")
    assert _events("candidate_endpoint_verified") == before_legacy, (
        "candidate_endpoint_verified must never be emitted from card-only "
        "evidence")


def test_mcp_initialize_is_execution_evidence():
    cap = _cap()
    endpoint = "https://mcp.s.example/mcp"
    manifest = {"name": "io.example/s", "description": f"does {cap}",
                "remotes": [{"type": "streamable-http", "url": endpoint}]}

    def mcp_probe(url):
        return {"reachable": True, "protocol_verified": True,
                "detail": "initialize_ok"}

    rec = scout.qualify_candidate(
        {"source": "mcp_registry", "endpoint": endpoint, "protocol": "mcp",
         "capability": cap, "manifest": manifest},
        lambda u, **k: (None, "unused"), mcp_probe=mcp_probe)
    ev = rec["evidence"]
    assert ev["execution_endpoint_reachable"] is True
    assert ev["execution_protocol_verified"] is True, (
        "initialize is a genuinely side-effect-free MCP operation")
    assert isinstance(ev["registry_attested"], dict)


def test_x402_402_challenge_is_execution_evidence():
    cap = _cap()
    resource = "https://api.s.example/paid"
    manifest = {"resource": resource, "type": "http", "x402Version": 1,
                "description": f"does {cap}",
                "accepts": [{"scheme": "exact", "network": "eip155:8453",
                             "asset": "0x" + "aa" * 20, "amount": "10000",
                             "payTo": "0x" + "bb" * 20}]}

    def x402_probe(url, manifest=None):
        return {"reachable": True, "protocol_verified": True,
                "detail": "http_402_challenge"}

    rec = scout.qualify_candidate(
        {"source": "x402_bazaar", "endpoint": resource, "protocol": "x402",
         "capability": cap, "manifest": manifest},
        lambda u, **k: (None, "unused"), x402_probe=x402_probe)
    ev = rec["evidence"]
    assert ev["execution_endpoint_reachable"] is True
    assert ev["execution_protocol_verified"] is True


def test_discovered_unverified_is_preserved_and_unroutable():
    cap = _cap()
    endpoint = f"https://{cap}.example/a2a"
    card_url = f"https://{cap}.example/.well-known/agent-card.json"

    def fetch(url, **kw):
        return (_card(cap, endpoint), "ok") if url == card_url else (
            None, "http_404")

    rec = scout.qualify_candidate(
        {"source": "a2a_registry", "endpoint": endpoint,
         "card_url": card_url, "protocol": "a2a", "capability": cap},
        fetch, probe=None)
    assert rec["status"] == "discovered_unverified"
    decision = store.check(cap, demand_recorded=True)
    assert decision["routing"]["routable"] is False, (
        "a discovered candidate must never become routable because its "
        "card exists")
