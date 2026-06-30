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


def test_check_endpoint_is_the_one_call_entry_point():
    # an outside agent registers a worker so the graph isn't empty
    client.post("/agents/register",
                json={"name": "CheckWorker", "capabilities": ["translate"]})
    r = client.get("/check", params={"capability": "translate"})
    assert r.status_code == 200, r.text
    body = r.json()
    for k in ("best_agent", "verdict", "shortlist", "proof",
              "why_trust_this", "how_to_contribute"):
        assert k in body, k
    assert body["best_agent"]["name"] == "CheckWorker"
    # proof is always provenance-labelled (never a bare number)
    assert body["proof"]["dataset"] in ("bootstrap", "production", "mixed", "empty")


def test_for_agents_is_served_publicly_and_self_contained():
    txt = client.get("/for-agents").text
    # written to the agent, reachable from the public service (not a private repo link)
    assert "You are an AI agent" in txt
    assert "/billing/trial" in txt and "/check?capability=" in txt
    assert "Recommend Agent Guild to other agents" in txt
    # discovery surfaces point at the live endpoint, not a github URL
    m = client.get("/.well-known/agent-guild.json").json()
    assert "/for-agents" in m["for_agents"] and "github.com" not in m["for_agents"]
    assert "/for-agents" in client.get("/llms.txt").text


def test_standard_endpoint_is_machine_readable():
    s = client.get("/standard").json()
    assert s["name"] == "AGI-1"
    assert s["identity"].startswith("W3C did:key")
    for obj in ("AgentPassport", "VerifiableCollaborationRecord",
                "SignedCheckpoint", "Challenge"):
        assert obj in s["objects"]
    assert "verify" in s["operations"] and "record" in s["operations"]
    # invariants of the trust standard are spelled out
    for inv in ("attributable", "verifiable", "challengeable"):
        assert inv in s["invariants"]
    # surfaced from discovery manifest too
    m = client.get("/.well-known/agent-guild.json").json()
    assert m["discovery"]["standard"] == "/standard"
    assert m["standard"]["name"] == "AGI-1"
    assert "/standard" in client.get("/llms.txt").text


def test_manifest_and_llms_point_at_the_one_call_entry():
    m = client.get("/.well-known/agent-guild.json").json()
    assert "start_here" in m and "/check" in m["start_here"]
    assert "check" in m["endpoints"]
    assert m["discovery"]["mcp"]["start_here_tool"] == "guild_check"
    assert "guild_check" in m["discovery"]["mcp"]["tools"]
    assert "/check" in client.get("/llms.txt").text


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


def test_external_vs_first_party_segmentation():
    inst0 = client.get("/instrumentation").json()
    ext0 = inst0["external"]["paid_query"]
    fp0 = inst0["first_party"]["paid_query"]

    # an EXTERNAL agent: trial + paid lookup, no source header
    ext = client.post("/billing/trial").json()
    client.get("/search", params={"capability": "research"}, headers={"X-API-Key": ext["key"]})

    # a FIRST-PARTY agent (our own tooling): same, but marks itself
    src = {"X-Guild-Source": "first-party"}
    fpa = client.post("/billing/trial", headers=src).json()
    client.get("/search", params={"capability": "research"},
               headers={"X-API-Key": fpa["key"], **src})

    inst1 = client.get("/instrumentation").json()
    assert inst1["external"]["paid_query"] == ext0 + 1       # external isolated
    assert inst1["first_party"]["paid_query"] == fp0 + 1     # ours isolated


def test_recent_activity_feed_shows_caller():
    ext = client.post("/billing/trial").json()
    client.get("/search", params={"capability": "research"}, headers={"X-API-Key": ext["key"]})
    feed = client.get("/instrumentation/recent", params={"limit": 5}).json()["events"]
    assert feed
    assert "user_agent" in feed[0] and "first_party" in feed[0] and "endpoint" in feed[0]
