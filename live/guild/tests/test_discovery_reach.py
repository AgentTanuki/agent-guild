"""Honest, durable and signed autonomous-agent discovery measurement."""
from __future__ import annotations

import hashlib
import json

from fastapi.testclient import TestClient

from app import attribution, crypto, main
from app.store import Store


def test_reach_deduplicates_agents_and_excludes_vanity_traffic(tmp_path):
    store = Store(path=str(tmp_path / "guild.json"))
    for _ in range(6):
        store.record_event(
            "http:real-one", "discovery_resource_fetched",
            ua="langchain/0.2.1", discovery_surface="ard_catalog",
            actor_distinct=True,
        )
    store.record_event(
        "mcp:real-two", "paid_offer_served",
        ua="mcp:independent-agent/1.0", source="paid_offer:mcp_tool",
        operation="best_agent", actor_distinct=True,
    )
    store.record_event(
        "http:crawler", "discovery_resource_fetched",
        ua="Smithery crawler/1.0", discovery_surface="ard_catalog",
        actor_distinct=True,
    )
    store.record_event(
        "http:tool", "discovery_resource_fetched", ua="curl/8.7.1",
        discovery_surface="ard_catalog", actor_distinct=True,
    )
    store.record_event(
        None, "discovery_resource_fetched", ua="langchain/0.2.1",
        discovery_surface="ard_catalog", actor_distinct=False,
    )
    store.record_event(
        "http:ours", "discovery_resource_fetched",
        ua="langchain/0.2.1", discovery_surface="ard_catalog",
        actor_distinct=True, fp=True,
    )

    report = store.discovery_reach(target=2)
    assert report["qualified_distinct_autonomous_agents"] == 2
    assert report["target_achieved"] is True
    assert report["remaining"] == 0
    assert report["evidence"]["directly_instrumented_distinct_actors"] == 1
    assert report["evidence"][
        "pre_resource_instrumentation_distinct_actors"] == 1
    assert report["evidence"]["registry_crawler_distinct_actors_excluded"] == 1
    assert report["evidence"]["direct_resource_fetches"] == 10
    assert report["evidence"]["legacy_catalogue_rows_not_treated_as_fetches"] == 1

    proof = report["proof"]
    public_key = crypto.public_key_from_did(proof["verification_key"])
    assert crypto.verify_jcs(proof["payload"], proof["signature"], public_key)
    assert proof["payload"]["actor_evidence_rows"] == 2
    assert report["tiers"] == {
        "T1_key_proved_members": 0,
        "T2_named_mcp_clients": 1,
        "T3_framework_ua_actors": 1,
    }

    replay = store.discovery_reach(target=2, include_actor_evidence=True)
    rows = replay["actor_evidence"]
    assert all("actor_alias_sha256" in row and "actor" not in row
               for row in rows)
    canonical = json.dumps(
        rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert hashlib.sha256(canonical.encode()).hexdigest() == report[
        "evidence"]["actor_evidence_set_sha256"]


def test_machine_resource_fetch_records_one_noncommercial_observation():
    client = TestClient(main.app)
    before = len(main.store.events)
    response = client.get(
        "/.well-known/ai-catalog.json",
        headers={"user-agent": "langgraph-autonomous-agent/1.0"},
    )
    assert response.status_code == 200
    new = main.store.events[before:]
    observations = [event for event in new
                    if event["type"] == "discovery_resource_fetched"]
    assert len(observations) == 1
    assert observations[0]["discovery_surface"] == "ard_catalog"
    assert not any(event["type"] in {
        "capability_demand", "offer_served", "paid_offer_served",
        "paid_offer_shown",
    } for event in new)


def test_agent_skill_identity_is_transparent_and_qualified(tmp_path):
    store = Store(path=str(tmp_path / "guild.json"))
    store.record_event(
        "http:skill-user", "query",
        ua="agentguild-skill/1.0 (host=openclaw)",
        endpoint="check", actor_distinct=True,
    )
    report = store.discovery_reach()
    assert report["qualified_distinct_autonomous_agents"] == 1
    assert report["tiers"]["T3_framework_ua_actors"] == 1


def test_legacy_guild_owned_member_is_excluded_at_read_and_write_time(tmp_path):
    """The public Codex worker used to be one of the signed census's T1 rows.

    Its legacy account and immutable events predate durable first-party
    tagging.  The exact owned ID must demote those historical rows at read time
    and stamp every future event first-party, without mutating history.
    """
    store = Store(path=str(tmp_path / "guild.json"))
    owned_id = "agent_c7d2e902dc50"
    assert owned_id in attribution.KNOWN_GUILD_OPERATED_AGENT_IDS
    store.agents[owned_id] = {
        "id": owned_id,
        "milestones": {"key_proof": {"at": "2026-07-27T10:13:00Z"}},
    }
    store.accounts["owned-worker-key"] = {
        "owner_agent_id": owned_id,
        "first_party": False,
    }
    historical = {
        "key": "owned-worker-key",
        "type": "key_proof",
        "ua": "python-httpx/0.27",
        "fp": False,
        "actor_distinct": True,
        "at": "2026-07-27T10:13:41+00:00",
    }
    store.events.append(historical)

    report = store.discovery_reach()
    assert report["qualified_distinct_autonomous_agents"] == 0
    assert report["evidence"]["excluded_distinct_actors_by_reason"][
        "first_party"] == 1
    assert historical["fp"] is False, "historical evidence must stay immutable"

    store.record_event(
        "owned-worker-key", "query", ua="python-httpx/0.27",
        actor_distinct=True,
    )
    assert store.events[-1]["fp"] is True


def test_nearby_unowned_verified_member_still_counts(tmp_path):
    """The corrective set is exact: no name/domain/agent-id prefix matching."""
    store = Store(path=str(tmp_path / "guild.json"))
    stranger_id = "agent_c7d2e902dc51"
    store.agents[stranger_id] = {
        "id": stranger_id,
        "milestones": {"key_proof": {"at": "2026-08-16T00:00:00Z"}},
    }
    store.accounts["stranger-key"] = {"owner_agent_id": stranger_id}
    store.record_event(
        "stranger-key", "query", ua="python-httpx/0.27",
        actor_distinct=True,
    )
    report = store.discovery_reach()
    assert report["qualified_distinct_autonomous_agents"] == 1
    assert report["tiers"]["T1_key_proved_members"] == 1


def test_unproved_member_cannot_bypass_t1_with_a_framework_ua(tmp_path):
    """T1 requires key proof; a plausible UA cannot upgrade a bare member."""
    store = Store(path=str(tmp_path / "guild.json"))
    store.agents["agent_external_unproved"] = {
        "id": "agent_external_unproved",
        "milestones": {},
    }
    store.accounts["unproved-key"] = {
        "owner_agent_id": "agent_external_unproved",
    }
    store.record_event(
        "unproved-key", "query", ua="langchain/0.3",
        actor_distinct=True,
    )
    report = store.discovery_reach()
    assert report["qualified_distinct_autonomous_agents"] == 0
    assert report["evidence"]["excluded_distinct_actors_by_reason"][
        "authenticated_but_key_unproved"] == 1


def test_ard_has_every_free_web_discovery_hook():
    client = TestClient(main.app)
    robots = client.get("/robots.txt").text
    assert "Agentmap: https://agent-guild-5d5r.onrender.com/" \
        ".well-known/ai-catalog.json" in robots
    html = client.get("/", headers={"accept": "text/html"}).text
    assert '<link rel="ai-catalog" href="/.well-known/ai-catalog.json">' \
        in html
    for crawler in ("GPTBot", "ChatGPT-User", "ClaudeBot", "anthropic-ai",
                    "Google-Extended", "PerplexityBot", "Applebot-Extended",
                    "CCBot"):
        assert f"User-agent: {crawler}\nAllow: /" in robots

    marker = '<script type="application/ld+json">'
    schema_text = html.split(marker, 1)[1].split("</script>", 1)[0]
    schema = json.loads(schema_text)
    assert schema["@context"] == "https://schema.org"
    assert schema["@type"] == "SoftwareApplication"
    assert schema["name"] == "Agent Guild"
    assert "autonomous agents" in schema["description"]


def test_402index_domain_verification_is_exact_public_text():
    client = TestClient(main.app)
    response = client.get("/.well-known/402index-verify.txt")
    assert response.status_code == 200
    assert response.text == (
        "07302f2156b7828c1fe776c5910a0828a87be242bbc84352358745cca1a13091"
    )
    assert response.headers["content-type"].startswith("text/plain")
    assert "public" in response.headers["cache-control"]


def test_public_reach_endpoint_is_signed_and_excludes_its_own_current_fetch():
    client = TestClient(main.app)
    response = client.get(
        "/discovery/reach",
        headers={"user-agent": "langgraph-proof-reader/1.0"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["target"] == 25_000
    assert body["metric"] == (
        "distinct attributable autonomous-agent discoverers")
    assert body["proof"]["payload"]["target_distinct_autonomous_agents"] == 25_000
    public_key = crypto.public_key_from_did(body["proof"]["verification_key"])
    assert crypto.verify_jcs(
        body["proof"]["payload"], body["proof"]["signature"], public_key)
    snapshot_events = body["proof"]["payload"]["event_snapshot_rows"]
    evidence = client.get(
        "/discovery/reach/evidence?limit=2000&snapshot_events="
        f"{snapshot_events}").json()
    assert evidence["total"] == body[
        "qualified_distinct_autonomous_agents"]
    assert evidence["event_snapshot_rows"] == snapshot_events
    assert all("actor_alias_sha256" in row for row in evidence[
        "actor_evidence"])
