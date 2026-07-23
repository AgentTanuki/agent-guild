"""POST /feedback/abandonment — self-reported funnel drop-off with reasons
(passport programme 2026-07-23).

Authenticated (X-API-Key, same resolution rule as /demand/watch), closed
reason_code enum, bounded free text. Records an `abandonment_reported` event
the passport funnel aggregates; it never moves trust, standing or pricing.
"""
import os

os.environ.setdefault("GUILD_DATA", "")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.state import store  # noqa: E402

client = TestClient(app)


def _register(name="AbandonAgent"):
    r = client.post("/agents/register",
                    json={"name": name, "capabilities": ["x"]})
    assert r.status_code == 200
    return r.json()


def test_auth_is_required():
    body = {"stage": "prove", "reason_code": "proof_too_hard"}
    assert client.post("/feedback/abandonment", json=body).status_code == 401
    assert client.post("/feedback/abandonment", json=body,
                       headers={"X-API-Key": "sk_bogus"}).status_code == 401


def test_reason_code_enum_is_enforced():
    reg = _register("AbandonEnum")
    r = client.post("/feedback/abandonment",
                    json={"stage": "prove", "reason_code": "sun_in_my_eyes"},
                    headers={"X-API-Key": reg["api_key"]})
    assert r.status_code == 422
    # bounds: stage ≤ 32, detail ≤ 500
    r = client.post("/feedback/abandonment",
                    json={"stage": "s" * 33, "reason_code": "other"},
                    headers={"X-API-Key": reg["api_key"]})
    assert r.status_code == 422
    r = client.post("/feedback/abandonment",
                    json={"stage": "x", "reason_code": "other",
                          "detail": "d" * 501},
                    headers={"X-API-Key": reg["api_key"]})
    assert r.status_code == 422


def test_report_records_event_and_returns_guild_next():
    reg = _register("AbandonReporter")
    n0 = len(store.events)
    r = client.post("/feedback/abandonment",
                    json={"stage": "passport",
                          "reason_code": "no_counterparty",
                          "detail": "nobody to show the credential to yet"},
                    headers={"X-API-Key": reg["api_key"]})
    assert r.status_code == 200
    body = r.json()
    assert body["recorded"] is True
    assert "primary" in body["guild_next"]      # same shape as other authed replies
    ev = next(e for e in store.events[n0:]
              if e.get("type") == "abandonment_reported")
    assert ev["stage"] == "passport"
    assert ev["reason_code"] == "no_counterparty"
    assert ev["detail"] == "nobody to show the credential to yet"
    assert ev["key"] not in (None, "anon")      # attributable to the reporter


def test_reports_aggregate_by_reason_code():
    reg = _register("AbandonAggregator")
    h = {"X-API-Key": reg["api_key"]}
    for _ in range(2):
        assert client.post("/feedback/abandonment",
                           json={"stage": "discovery",
                                 "reason_code": "no_relevant_supply"},
                           headers=h).status_code == 200
    counts = client.get("/funnel/passports").json()["abandonment"]
    assert counts.get("no_relevant_supply", 0) >= 2
