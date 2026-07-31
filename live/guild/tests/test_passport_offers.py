"""The passport-lead CTA and its offer telemetry (passport programme
2026-07-23).

Every discovery surface that carries the passport CTA in its payload records
ONE `offer_served` event (offer='passport', endpoint=<surface>) at the serve
point: llms.txt, the manifest, the A2A agent card, anonymous /check,
/capabilities, /demand/feed, and the x402 402 challenge. The CTA text itself
leads with the free register → prove → passport path, carries the exact calls
inline, and instructs callers to tag their register with
src='passport_offer:<surface>' so following the offer is attributable.
Payment/escrow text never leads.
"""
import os

os.environ.setdefault("GUILD_DATA", "")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.state import store  # noqa: E402

client = TestClient(app)


def _offers_since(n0, endpoint):
    return [e for e in store.events[n0:]
            if e.get("type") == "offer_served" and e.get("endpoint") == endpoint]


def test_llms_txt_leads_with_passport_and_records_offer():
    n0 = len(store.events)
    r = client.get("/llms.txt")
    assert r.status_code == 200
    text = r.text
    # the passport claim LEADS: it appears before the /check pitch
    assert text.index("Agent Passport") < text.index("Start here")
    assert 'src": "passport_offer:llms' in text
    assert "POST /agents/{id}/prove" in text
    assert "GET /agents/{id}/passport" in text
    offers = _offers_since(n0, "llms")
    assert len(offers) == 1 and offers[0]["offer"] == "passport"


def test_manifest_leads_with_passport_and_records_offer():
    n0 = len(store.events)
    r = client.get("/.well-known/agent-guild.json")
    assert r.status_code == 200
    m = r.json()
    assert "passport_offer:manifest" in " ".join(m["claim_passport"]["calls"])
    assert "Passport" in m["description"]
    # payment info is retained but demoted below the passport block
    keys = list(m.keys())
    assert keys.index("claim_passport") < keys.index("payments")
    offers = _offers_since(n0, "manifest")
    assert len(offers) == 1 and offers[0]["offer"] == "passport"


def test_agent_card_leads_with_passport_and_records_offer():
    n0 = len(store.events)
    r = client.get("/.well-known/agent-card.json",
                   headers={"User-Agent": "card-bot/1.0"})
    assert r.status_code == 200
    desc = r.json()["description"]
    assert "passport_offer:agent_card" in desc
    assert desc.index("Passport") < desc.index("check:")
    offers = _offers_since(n0, "agent_card")
    assert len(offers) == 1 and offers[0]["offer"] == "passport"
    # a2a attribution conventions: derived a2a: actor key + tagged real UA
    assert offers[0]["key"].startswith("a2a:")
    assert offers[0]["ua"] == "a2a:card-bot/1.0"
    assert offers[0]["surface"] == "a2a"


def test_anonymous_check_carries_passport_offer():
    n0 = len(store.events)
    r = client.get("/check", params={"capability": "offer-probe-cap"})
    assert r.status_code == 200
    body = r.json()
    assert list(body.keys())[0] == "claim_passport"       # the offer LEADS
    assert "passport_offer:check" in body["claim_passport"]["register"]
    offers = _offers_since(n0, "check")
    assert len(offers) == 1 and offers[0]["offer"] == "passport"


def test_authenticated_check_gets_no_anonymous_offer():
    reg = client.post("/agents/register",
                      json={"name": "OfferAuthed", "capabilities": ["x"]}).json()
    n0 = len(store.events)
    r = client.get("/check", params={"capability": "offer-probe-cap"},
                   headers={"X-API-Key": reg["api_key"]})
    assert r.status_code == 200
    assert "claim_passport" not in r.json()
    assert _offers_since(n0, "check") == []


def test_capabilities_and_demand_feed_lead_with_passport():
    n0 = len(store.events)
    r = client.get("/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert list(body.keys())[0] == "claim_passport"
    assert "passport_offer:capabilities" in body["claim_passport"]["register"]
    assert len(_offers_since(n0, "capabilities")) == 1

    n1 = len(store.events)
    r = client.get("/demand/feed")
    assert r.status_code == 200
    sp = r.json()["supplier_path"]
    assert list(sp.keys())[0] == "claim_passport"
    assert sp["claim_passport"]["register"]["body"]["src"] == \
        "passport_offer:demand_feed"
    assert len(_offers_since(n1, "demand_feed")) == 1
    # a 304 re-serves nothing, so it must not count another offer
    etag = r.headers["ETag"]
    n2 = len(store.events)
    r304 = client.get("/demand/feed", headers={"If-None-Match": etag})
    assert r304.status_code == 304
    assert _offers_since(n2, "demand_feed") == []


def test_402_challenge_carries_passport_offer(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    n0 = len(store.events)
    r = client.get("/check", params={"capability": "paid-offer-cap"})
    assert r.status_code == 402
    detail = r.json()["detail"]
    cp = detail["claim_passport"]
    assert "passport_offer:x402_challenge" in cp["register"]
    assert "never" in cp["note"]        # payment buys reads, never membership
    offers = _offers_since(n0, "x402_challenge")
    assert len(offers) == 1 and offers[0]["offer"] == "passport"


def test_offer_served_is_guild_surfacing_never_engagement():
    """A crawler with a framework UA hammering llms.txt must NOT trip the
    engaged-external detector off the offers WE serve it (the same honesty
    rule as prove_surfaced)."""
    from app.attribution import engagement_kind, GUILD_SURFACING_TYPES
    assert "offer_served" in GUILD_SURFACING_TYPES
    assert engagement_kind({"type": "offer_served", "ua": "python-httpx/0.28",
                            "offer": "passport"}) == "guild_surfacing"
