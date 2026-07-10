"""Discovery swarm — discovery agents: action ledger, budgets, allowlist,
kill switch; ecosystem map; utility model; stats/graph endpoints."""
import os
os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, store  # noqa: E402
from app.swarm import agents as swarm_agents  # noqa: E402
from app.swarm.router import ensure_built  # noqa: E402

client = TestClient(app)


def setup_module():
    ensure_built()
    store.swarm_state["killed"] = False
    os.environ.pop("GUILD_SWARM_KILL", None)


def test_agent_status_lists_five_bounded_mandates():
    r = client.get("/swarm/agents").json()
    names = {a["name"] for a in r["agents"]}
    assert names == {"verifier", "publisher", "gap-scout", "interop-tester",
                     "wording-optimizer"}
    assert all(a["daily_action_budget"] == swarm_agents.DAILY_ACTION_BUDGET
               for a in r["agents"])
    assert all(u.startswith("https://") for u in r["allowlist"])


def test_internal_agents_tick_logs_reason_coded_actions():
    # run only the internal (no-network) mandates
    r = client.post("/swarm/agents/run",
                    json={"agents": ["publisher", "gap-scout",
                                     "wording-optimizer", "interop-tester"]})
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results["publisher"]["unpublished_capabilities"] == []
    assert "capability_proposals" in results["gap-scout"]
    assert results["interop-tester"]["outcome"] == "ok", results["interop-tester"]
    actions = store.swarm_state["actions"]
    assert actions, "actions must be ledgered"
    a = actions[-1]
    for key in ("at", "agent", "reason_code", "target", "protocol", "outcome",
                "attribution", "policy_decision", "retry_state", "cost"):
        assert key in a, key
    assert a["attribution"] == "ag_internal"


def test_allowlist_blocks_non_allowlisted_urls():
    import httpx
    with httpx.Client() as c:
        resp = swarm_agents._guarded_fetch(store, "verifier", "test",
                                           "https://evil.example.com/x", c)
    assert resp is None
    assert store.swarm_state["actions"][-1]["policy_decision"] == "not_allowlisted"


def test_budget_exhaustion_blocks_actions():
    original = swarm_agents.DAILY_ACTION_BUDGET
    swarm_agents.DAILY_ACTION_BUDGET = 0
    try:
        import httpx
        with httpx.Client() as c:
            resp = swarm_agents._guarded_fetch(
                store, "verifier", "test",
                "https://registry.modelcontextprotocol.io/v0/servers", c)
        assert resp is None
        assert store.swarm_state["actions"][-1]["policy_decision"] == "budget_exhausted"
    finally:
        swarm_agents.DAILY_ACTION_BUDGET = original


def test_kill_switch_halts_discovery_agents():
    store.swarm_state["killed"] = True
    try:
        r = client.post("/swarm/agents/run", json={"agents": ["publisher"]})
        assert r.json().get("blocked") == "kill_switch"
    finally:
        store.swarm_state["killed"] = False


def test_ecosystem_map_shape():
    r = client.get("/swarm/ecosystems").json()
    assert len(r["ecosystems"]) >= 5
    for eco in r["ecosystems"]:
        for key in ("id", "protocol", "registration_method", "search_method",
                    "auth", "rate_limits", "terms", "ag_coverage",
                    "adapter_health", "demand_signals"):
            assert key in eco, key


def test_utility_match_ranks_relevant_capability_first():
    r = client.get("/swarm/match", params={
        "task": "repair broken malformed json output from an llm"}).json()
    assert r["matches"][0]["capability"] == "json.repair"
    f = r["matches"][0]["factors"]
    for key in ("semantic_fit", "historical_accuracy", "success_probability",
                "latency", "cost", "availability", "trust_attestation",
                "context_fit", "privacy_fit", "dependency_complexity",
                "failure_recovery", "composability"):
        assert key in f


def test_stats_and_graph_endpoints():
    s = client.get("/swarm/stats").json()
    assert "genuine_external" in s["growth"]
    assert "ag_internal_first_party" in s["growth"]
    assert s["limits"]["max_payload_bytes"] == 65536
    g = client.get("/swarm/graph").json()
    assert "actors" in g and "registrations_via_referral" in g


def test_dashboard_renders():
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "Machine Growth" in r.text
    assert "genuine external" in r.text.lower()
