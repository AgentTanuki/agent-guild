"""B4 — the autonomous machine conversion funnel.

`korean-legal` (the observed production demand case) walks the pipeline:
demand observed (unpaid external ask) → scout discovers a candidate →
candidate endpoint verified → feed pulled → supplier registers → declares an
endpoint → paid decision. The funnel counts every stage; AG-owned probes,
release gates, canaries and registry crawlers are STRUCTURALLY excluded from
external stages; mainnet_settlement is honestly zero; no Korean legal
provider is fabricated and no unverified agent is invoked or hired.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import x402
from app.state import store
from app.swarm import scout

PAY_TO = "0x" + "11" * 20
EXT_UA = "external-agent-framework/1.4 (langgraph)"


@pytest.fixture(autouse=True)
def _enforced(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    yield


def _stage(funnel, name):
    return next(s for s in funnel["stages"] if s["stage"] == name)


def test_korean_legal_walks_the_funnel_without_fabrication():
    from app.main import app
    cap = "korean-legal"
    with TestClient(app) as client:
        base = client.get("/funnel").json()
        b = {s["stage"]: s["count"] for s in base["stages"]}

        # 1. demand observed: the real case — an unpaid external A2A ask
        r = client.post("/a2a", json={
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"role": "user",
                                   "parts": [{"kind": "text",
                                              "text": f"check: {cap}"}]}},
        }, headers={"User-Agent": EXT_UA})
        assert r.json()["result"]["kind"] == "task"     # x402 challenge fired

        # 2./3. scout discovers + verifies a candidate — from observed demand
        endpoint = "https://kr-legal.example"

        def fetch(url, **kw):
            if "a2aregistry.org" in url:
                return ([{"name": "kr-legal", "url": endpoint,
                          "description": "korean-legal analysis"}], "ok")
            if url.startswith(endpoint):
                return ({"name": "kr-legal", "url": endpoint,
                         "skills": [{"id": cap}]}, "ok")
            return ({"servers": [], "items": []}, "ok")

        scout.run_scout(store, fetch=fetch,
                        probe=lambda u: {"reachable": True, "detail": "ok"})

        # the discovered candidate was NOT invoked, hired, or registered
        assert not any(a["name"] == "kr-legal" for a in store.agents.values())
        key = f"a2a_registry:{endpoint}"
        assert store.swarm_state["scout"]["candidates"][key]["status"] == \
            "discovered_unverified"

        # 4. a supplier machine pulls the feed and registers ITSELF
        feed = client.get("/demand/feed",
                          headers={"User-Agent": EXT_UA}).json()
        assert any(e["capability"] == cap for e in feed["entries"])
        reg = client.post("/agents/register",
                          json={"name": "kr-legal-supplier",
                                "capabilities": [cap]},
                          headers={"User-Agent": EXT_UA})
        assert reg.status_code == 200

        f = client.get("/funnel").json()
        c = {s["stage"]: s["count"] for s in f["stages"]}
        assert c["demand_observed"] == b["demand_observed"] + 1
        assert c["candidate_discovered"] >= b["candidate_discovered"] + 1
        # a2a candidate: its CARD proves discovery only — endpoint (execution)
        # verification honestly does NOT fire from card-only evidence.
        assert c["candidate_endpoint_verified"] == \
            b["candidate_endpoint_verified"]
        assert c["pulled_feed"] == b["pulled_feed"] + 1
        assert c["registered"] == b["registered"] + 1
        # honesty: real EXTERNAL mainnet settlements remain zero (and a
        # first-party canary could never masquerade as one)
        assert c["external_mainnet_settlement"] == 0


def test_ag_owned_traffic_is_structurally_excluded():
    from app.main import app
    cap = "funnel-excl-" + uuid.uuid4().hex[:6]
    with TestClient(app) as client:
        before = {s["stage"]: s["count"]
                  for s in client.get("/funnel").json()["stages"]}
        # AG-owned probe (first-party header) + registry crawler + AG test UA
        client.get(f"/check?capability={cap}",
                   headers={"User-Agent": "guild-release-gate/1",
                            "X-Guild-Source": "release-gate"})
        client.get(f"/check?capability={cap}2",
                   headers={"User-Agent": "Smithery-Scanner/1 (+bot)"})
        client.get("/demand/feed",
                   headers={"User-Agent": "guild-selfcheck/1",
                            "X-Guild-Source": "ops-watch"})
        after = {s["stage"]: s["count"]
                 for s in client.get("/funnel").json()["stages"]}
        assert after["demand_observed"] == before["demand_observed"]
        assert after["pulled_feed"] == before["pulled_feed"]


def test_feed_pull_polling_does_not_flood_the_funnel():
    from app.main import app
    with TestClient(app) as client:
        before = {s["stage"]: s["count"]
                  for s in client.get("/funnel").json()["stages"]}
        for _ in range(10):                      # a poller, same actor
            client.get("/demand/feed", headers={"User-Agent": EXT_UA})
        after = {s["stage"]: s["count"]
                 for s in client.get("/funnel").json()["stages"]}
        assert after["pulled_feed"] <= before["pulled_feed"] + 1


def test_paid_decision_stage_counts_external_paid_reads(monkeypatch):
    from app.main import app
    from tests.test_x402_v2 import FakeFacilitator, make_payload, sig_header
    from app import payments
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    cap = "funnel-paid-" + uuid.uuid4().hex[:6]
    with TestClient(app) as client:
        before = {s["stage"]: s["count"]
                  for s in client.get("/funnel").json()["stages"]}
        preq = payments.check_request(cap)
        r = client.get(f"/check?capability={cap}",
                       headers={"User-Agent": EXT_UA,
                                "PAYMENT-SIGNATURE":
                                sig_header(make_payload(preq))})
        assert r.status_code == 200
        after = {s["stage"]: s["count"]
                 for s in client.get("/funnel").json()["stages"]}
        assert after["paid_decision"] == before["paid_decision"] + 1
        assert after["external_mainnet_settlement"] == 0  # testnet ≠ revenue


def test_funnel_declares_its_exclusions_and_honest_zero():
    from app.main import app
    with TestClient(app) as client:
        f = client.get("/funnel").json()
        assert "structurally" in f["exclusions"]
        ms = _stage(f, "external_mainnet_settlement")
        assert ms["count"] == 0
        assert "zero" in ms["source"]
        # the canary stage exists and is explicitly not external revenue
        canary = _stage(f, "first_party_mainnet_canary")
        assert "NEVER external revenue" in canary["source"]
