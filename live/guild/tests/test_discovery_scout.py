"""B3 — the demand-driven discovery scout.

Invariants:
  * the scout searches ONLY against observed genuine unmet demand;
  * every discovered candidate is `discovered_unverified` — discovery never
    awards reputation, a hire verdict, evidence, or a registry entry;
  * hostile inputs are contained: SSRF (private/loopback endpoints),
    redirects, oversized responses and hostile card strings;
  * outbound contact is OFF by default; when enabled it is terms-gated,
    identity-disclosed, once per endpoint, once per capability per 24h, and
    the opt-out list is honoured.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.state import store
from app.swarm import scout

PAY_TO = "0x" + "11" * 20
EXT_UA = "external-agent-framework/2.0 (autogen)"


@pytest.fixture(autouse=True)
def _enforced(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    monkeypatch.delenv("GUILD_SCOUT_CONTACT", raising=False)
    yield


def _cap():
    return "scout-cap-" + uuid.uuid4().hex[:8]


def _observe_demand(cap):
    from app.main import app
    with TestClient(app) as client:
        client.get(f"/check?capability={cap}", headers={"User-Agent": EXT_UA})


def _card(cap, endpoint):
    return {"name": f"supplier-of-{cap}", "url": endpoint,
            "skills": [{"id": cap}], "protocolVersion": "0.3.0"}


def _fake_fetch_factory(cap, endpoint, card=None):
    """Registry + card fetches, fully stubbed — no network."""
    card = card if card is not None else _card(cap, endpoint)

    def fetch(url, max_bytes=scout.MAX_CARD_BYTES):
        if "a2aregistry.org" in url:
            return ([{"name": f"supplier-of-{cap}", "url": endpoint,
                      "description": f"does {cap}",
                      "wellKnownURI": endpoint +
                      "/.well-known/agent-card.json"}], "ok")
        if "registry.modelcontextprotocol.io" in url:
            return ({"servers": []}, "ok")
        if "discovery/resources" in url:
            return ({"items": []}, "ok")
        if url.startswith(endpoint):
            return (card, "ok")
        return (None, "http_404")
    return fetch


def _probe_ok(url):
    return {"reachable": True, "status": "ok", "detail": "200"}


def test_scout_searches_only_observed_unmet_demand():
    cap = _cap()
    calls = []

    def adapter(capability, fetch):
        calls.append(capability)
        return []

    # nothing observed → nothing searched
    summary = scout.run_scout(store, fetch=lambda u, **k: (None, "no"),
                              adapters={"x": adapter})
    assert cap not in calls
    # observed genuine demand → searched
    _observe_demand(cap)
    scout.run_scout(store, fetch=lambda u, **k: (None, "no"),
                    adapters={"x": adapter})
    assert cap in calls


def test_discovered_candidate_is_unverified_and_earns_nothing():
    cap = _cap()
    endpoint = "https://supplier.example"
    _observe_demand(cap)
    agents_before = set(store.agents)
    summary = scout.run_scout(store,
                              fetch=_fake_fetch_factory(cap, endpoint),
                              probe=_probe_ok)
    assert summary["discovered"] >= 1
    key = f"a2a_registry:{endpoint}"
    rec = store.swarm_state["scout"]["candidates"][key]
    assert rec["status"] == "discovered_unverified"
    assert rec["card_valid"] is True
    assert rec["endpoint_reachable"] is True
    assert rec["source"] == "a2a_registry" and rec["last_seen"]
    # discovery NEVER creates an agent, reputation, verdict or evidence
    assert set(store.agents) == agents_before
    assert not store.attestations_for("supplier-of-" + cap)
    # and the candidate is not routable via /check
    decision = store.check(cap, demand_recorded=True)
    assert decision["routing"]["routable"] is False


def test_candidate_stays_unverified_until_cryptographic_participation():
    """Acceptance gate: only registering + proving (cryptographic
    participation) moves a discovered supplier into the trust plane — the
    scout record itself never upgrades."""
    cap = _cap()
    endpoint = "https://supplier2.example"
    _observe_demand(cap)
    scout.run_scout(store, fetch=_fake_fetch_factory(cap, endpoint),
                    probe=_probe_ok)
    rec = store.swarm_state["scout"]["candidates"][
        f"a2a_registry:{endpoint}"]
    assert rec["status"] == "discovered_unverified"
    # the supplier itself registers (its own act, not the scout's)
    agent = store.register_agent(name="supplier2", capabilities=[cap],
                                 metadata={})
    assert agent["id"] in store.agents
    # the scout candidate record is UNCHANGED — no retroactive credit
    rec2 = store.swarm_state["scout"]["candidates"][
        f"a2a_registry:{endpoint}"]
    assert rec2["status"] == "discovered_unverified"


def test_ssrf_redirect_oversize_and_hostile_cards_are_contained():
    cap = _cap()
    _observe_demand(cap)
    # private/loopback endpoints refused at policy level
    for bad in ("https://127.0.0.1/x", "https://10.0.0.8/x",
                "http://169.254.169.254/latest/meta-data",
                "ftp://x.example/", "https://user:pw@host.example/"):
        rec = scout.qualify_candidate(
            {"endpoint": bad, "protocol": "a2a", "capability": cap},
            fetch=lambda u, **k: (None, "unreachable"))
        assert rec["endpoint_reachable"] is False
        assert rec["checks"]["endpoint_policy"] != "ok"
    # redirects + oversized responses are refusal reasons, not follows
    rec = scout.qualify_candidate(
        {"endpoint": "https://ok.example", "card_url": "https://ok.example",
         "protocol": "a2a", "capability": cap},
        fetch=lambda u, **k: (None, "redirect_refused"),
        probe=lambda u: {"reachable": False, "detail": "no"})
    assert rec["card_valid"] is False
    # hostile card: absurd strings are capped, wrong shapes rejected
    huge = "A" * 100_000
    rec = scout.qualify_candidate(
        {"endpoint": "https://ok.example", "card_url": "https://ok.example",
         "protocol": "a2a", "capability": cap},
        fetch=lambda u, **k: ({"name": huge, "url": huge,
                               "did": huge, "skills": [{"id": huge}]}, "ok"),
        probe=_probe_ok)
    assert rec["card_valid"] is True
    assert len(rec["card_facts"]["card_name"]) <= scout.MAX_STRING
    assert len(rec["bindings"]["declared_did"]) <= scout.MAX_STRING
    assert not isinstance(
        scout._validate_card(["not", "a", "dict"], "a2a")[1], list)


def test_contact_is_off_by_default_and_fully_gated(monkeypatch):
    cap = _cap()
    endpoint = "https://contactable.example"
    cand = {"capability": cap, "endpoint": endpoint}
    sent = []

    def send(ep, msg):
        sent.append((ep, msg))

    open_card = {"name": "x", "contact_policy": "open"}
    # 1. default OFF
    r = scout.maybe_contact(store, cand, open_card, send)
    assert r == {"contacted": False, "reason": "contact_disabled_default"}
    assert not sent
    # 2. enabled but terms absent → refused
    monkeypatch.setenv("GUILD_SCOUT_CONTACT", "1")
    r = scout.maybe_contact(store, cand, {"name": "x"}, send)
    assert r["reason"] == "terms_do_not_permit"
    # 3. terms permit → exactly one contact, identity disclosed, opt-out shown
    r = scout.maybe_contact(store, cand, open_card, send)
    assert r["contacted"] and r["delivered"]
    ep, msg = sent[0]
    assert ep == endpoint
    assert "Agent Guild" in msg["from"]["name"]          # disclosed identity
    assert msg["opt_out"]["how"]
    # 4. never the same endpoint twice
    r = scout.maybe_contact(store, cand, open_card, send)
    assert r["reason"] == "already_contacted_once"
    # 5. one candidate per capability per 24h
    cand2 = {"capability": cap, "endpoint": "https://other.example"}
    r = scout.maybe_contact(store, cand2, open_card, send)
    assert r["reason"] == "capability_rate_limited_24h"
    # 6. opt-out honoured permanently
    ep3 = "https://optout.example"
    scout.record_opt_out(store, ep3)
    r = scout.maybe_contact(store, {"capability": _cap(), "endpoint": ep3},
                            open_card, send, now=9e12)
    assert r["reason"] == "opted_out"
    assert len(sent) == 1                                # exactly one, ever


def test_erc8004_adapter_is_honestly_unsupported():
    assert scout.adapter_erc8004("anything", lambda u, **k: ({}, "ok")) == []
