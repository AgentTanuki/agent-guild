"""Adapter evidence classes (corrective pass).

Defects reproduced:
  * the scout generic-GETted an A2A EXECUTION endpoint (a JSON-RPC POST
    surface) and recorded the inevitable 404 as an A2A reachability
    failure;
  * evidence classes were conflated: what AG verified itself, what a
    registry attests, whether the endpoint independently answered, and
    whether the PROTOCOL was independently verified were one boolean;
  * registry-attested health could leak into AG-independent verification.

Rules under test:
  * never generic-GET an A2A execution endpoint; never invoke or create
    work on a discovered agent merely to test it — the A2A agent-card fetch
    (a read-only, side-effect-free discovery-protocol step) IS the
    independent evidence;
  * evidence is recorded in four separate classes;
  * candidate_endpoint_verified fires ONLY on AG-independent evidence.
"""
import uuid

import pytest

from app.state import store
from app.swarm import scout


def _cap():
    return "evid-cap-" + uuid.uuid4().hex[:8]


def _card(cap, endpoint):
    return {"name": f"supplier-of-{cap}", "url": endpoint,
            "skills": [{"id": cap}], "protocolVersion": "0.3.0"}


def test_a2a_execution_endpoint_is_never_generic_getted():
    cap = _cap()
    endpoint = "https://exec.supplier.example/a2a"
    card_url = "https://exec.supplier.example/.well-known/agent-card.json"
    fetched = []

    def fetch(url, **kw):
        fetched.append(url)
        if url == card_url:
            return (_card(cap, endpoint), "ok")
        return (None, "http_404")

    def forbidden_probe(url):
        raise AssertionError(
            f"generic probe of the A2A EXECUTION endpoint {url} — a 404 on "
            "GET is not an A2A failure and must never be recorded as one")

    rec = scout.qualify_candidate(
        {"source": "a2a_registry", "endpoint": endpoint,
         "card_url": card_url, "protocol": "a2a", "capability": cap},
        fetch, probe=forbidden_probe)
    assert rec["card_valid"] is True
    assert rec["endpoint_reachable"] is True, (
        "the successful side-effect-free agent-card fetch is the "
        "independent reachability evidence")
    assert endpoint not in fetched, "the execution endpoint was contacted"


def test_evidence_is_recorded_in_four_separate_classes():
    cap = _cap()
    endpoint = "https://s2.example/a2a"
    card_url = "https://s2.example/.well-known/agent-card.json"

    def fetch(url, **kw):
        return (_card(cap, endpoint), "ok") if url == card_url else (
            None, "http_404")

    rec = scout.qualify_candidate(
        {"source": "a2a_registry", "endpoint": endpoint,
         "card_url": card_url, "protocol": "a2a", "capability": cap,
         "registry_attested": {"health": "ok",
                               "lastChecked": "2026-07-14T00:00:00Z"}},
        fetch, probe=None)
    ev = rec["evidence"]
    assert ev["ag_verified"]["card_valid"] is True
    assert ev["registry_attested"] == {"health": "ok",
                                       "lastChecked": "2026-07-14T00:00:00Z"}
    assert ev["independently_reachable"] is True
    assert ev["protocol_verified"] is True     # valid A2A card handshake
    assert "attested by the registry" in ev["registry_attested_note"].lower()


def test_registry_attested_health_is_never_promoted():
    """Registry says healthy; AG's own card fetch fails. The candidate must
    NOT read as reachable/verified and candidate_endpoint_verified must not
    fire."""
    cap = _cap()
    endpoint = "https://dead.supplier.example/a2a"
    events_before = sum(1 for e in store.events
                        if e.get("type") == "candidate_endpoint_verified")

    def fetch(url, **kw):
        if "a2aregistry" in url:
            return ({"agents": [{"name": "dead", "url": endpoint,
                                 "description": f"does {cap}",
                                 "health": "ok"}]}, "ok")
        return (None, "fetch_failed: ConnectError")

    from app import demand
    demand.record_demand(cap, transport="http", actor="evd-1",
                         ua="external-agent-framework/2.0 (crewai)")
    scout.run_scout(store, fetch=fetch,
                    adapters={"a2a_registry": scout.adapter_a2a_registry})
    key = f"a2a_registry:{endpoint}"
    rec = store.swarm_state["scout"]["candidates"][key]
    assert rec["evidence"]["independently_reachable"] is False
    assert rec["evidence"]["protocol_verified"] is False
    events_after = sum(1 for e in store.events
                       if e.get("type") == "candidate_endpoint_verified")
    assert events_after == events_before, (
        "registry-attested health must never fire the AG-independent "
        "candidate_endpoint_verified event")


def test_mcp_evidence_classes_including_auth_gated():
    cap = _cap()
    endpoint = "https://mcp.s.example/mcp"
    manifest = {"name": "io.example/s", "description": f"does {cap}",
                "remotes": [{"type": "streamable-http", "url": endpoint}]}

    def mcp_probe_auth(url):
        return {"reachable": True, "protocol_verified": False,
                "detail": "auth_required_http_401"}

    rec = scout.qualify_candidate(
        {"source": "mcp_registry", "endpoint": endpoint, "protocol": "mcp",
         "capability": cap, "manifest": manifest},
        lambda u, **k: (None, "unused"), mcp_probe=mcp_probe_auth)
    ev = rec["evidence"]
    assert ev["ag_verified"]["card_valid"] is True
    assert ev["independently_reachable"] is True
    assert ev["protocol_verified"] is False
    assert "auth" in rec["checks"]["protocol_probe"]


def test_x402_evidence_classes():
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
    assert ev["independently_reachable"] is True
    assert ev["protocol_verified"] is True
