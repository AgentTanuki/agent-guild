"""Agent-native surface tests — discovery manifests, the trial faucet, the
machine-readable 402, and the instrumentation / evaluation endpoints."""
import os
os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.billing import TRIAL_CREDITS, PRICING  # noqa: E402

client = TestClient(app)


def test_discovery_manifest_describes_service():
    m = client.get("/.well-known/agent-guild.json").json()
    assert m["schema_version"] == "agent-guild/1"
    assert m["economics"]["pricing_credits"]["best_agent"] == PRICING["best_agent"]
    # an agent can learn how to acquire credits with no human
    assert m["economics"]["acquire_credits"]["trial"]["human_free"] is True
    assert "measured_lift" in m["evaluation_signals"]


def test_ai_plugin_and_llms_txt_present():
    p = client.get("/.well-known/ai-plugin.json").json()
    assert p["api"]["url"] == "/openapi.json"
    txt = client.get("/llms.txt").text
    assert "Agent Guild" in txt and "/billing/trial" in txt


def test_trial_faucet_is_human_free_and_funds_an_account():
    acct = client.post("/billing/trial").json()
    assert acct["balance"] >= TRIAL_CREDITS
    # the granted key actually works for a paid lookup
    r = client.get("/search", params={"capability": "research"},
                   headers={"X-API-Key": acct["key"]})
    assert r.status_code == 200
    assert int(r.headers["X-Guild-Balance"]) == acct["balance"] - PRICING["best_agent"]


def test_machine_readable_402_tells_an_agent_how_to_pay():
    os.environ["GUILD_BILLING_ENFORCED"] = "1"
    try:
        r = client.get("/search", params={"capability": "research"})
        assert r.status_code == 402
        detail = r.json()["detail"]
        assert detail["error"] == "payment_required"
        assert detail["acquire"]["trial"]["path"] == "/billing/trial"
    finally:
        os.environ.pop("GUILD_BILLING_ENFORCED", None)


def test_instrumentation_funnel_tracks_queries_and_delegations():
    # a fresh consumer: trial credits, a paid lookup, then a hire of the result.
    acct = client.post("/billing/trial").json()
    key = acct["key"]
    me = client.post("/agents/register", json={"name": "FunnelAgent", "capabilities": ["research"]}).json()
    worker = client.post("/agents/register", json={"name": "FunnelWorker", "capabilities": ["fact-check"]}).json()

    # two paid queries (first + repeat) on the same key
    client.get("/search", params={"capability": "fact-check"}, headers={"X-API-Key": key})
    client.get("/search", params={"capability": "fact-check"}, headers={"X-API-Key": key})
    # a delegation following a recommendation: search on the agent's own key, then hire
    client.get("/search", params={"capability": "fact-check"}, headers={"X-API-Key": me["api_key"]})
    client.post("/tasks", headers={"X-API-Key": me["api_key"]},
                json={"requester_id": me["id"], "worker_id": worker["id"], "task_type": "fact-check"})

    f = client.get("/instrumentation").json()
    assert f["paid_query"] >= 2
    assert f["repeat_paid_query_agents"] >= 1
    assert f["delegations"] >= 1


def test_evaluation_endpoint_returns_lift_shape():
    e = client.get("/evaluation").json()
    # keys present even before enough graded data exists
    for k in ("recommended_success_rate", "baseline_success_rate", "lift", "n_recommended"):
        assert k in e
