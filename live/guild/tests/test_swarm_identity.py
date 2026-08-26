"""Discovery swarm — identity factory: signed documents, publish gate, index,
A2A card + MCP exposure, manifest wiring."""
import os
os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, store  # noqa: E402
from app.crypto import verify_jcs  # noqa: E402
from app.swarm.capabilities import CAPABILITIES  # noqa: E402
from app.swarm.identity import IdentityRegistry, registry, build_identity  # noqa: E402
from app.swarm import readiness  # noqa: E402
from app.swarm.router import ensure_built  # noqa: E402

client = TestClient(app)


def setup_module():
    ensure_built()


def test_index_lists_all_published_identities():
    r = client.get("/.well-known/ag-identities/index.json")
    assert r.status_code == 200
    idx = r.json()
    assert idx["count"] == len(CAPABILITIES)
    entry = idx["identities"][0]
    for key in ("ag_id", "capability", "version", "invoke", "mcp_tool",
                "document", "health", "readiness_evidence"):
        assert key in entry
    assert idx["terms"].endswith("/terms.json")


def test_identity_document_is_complete_and_signed():
    idx = client.get("/.well-known/ag-identities/index.json").json()
    doc = client.get(f"/identities/{idx['identities'][0]['ag_id']}").json()
    ident = doc["identity"]
    # required fields from the brief
    for key in ("ag_id", "name", "capability", "protocols", "auth", "pricing",
                "expected_latency_ms", "reliability", "benchmark",
                "context_limits", "known_failure_modes", "prohibited_uses",
                "owner", "guild_membership", "created_at", "updated_at", "health"):
        assert key in ident, key
    assert ident["capability"]["input_schema"]["type"] == "object"
    assert ident["capability"]["version"]
    assert ident["benchmark"]["ok"] is True
    assert ident["health"] == "passing"
    assert ident["readiness_evidence"].endswith("/readiness")
    # signature verifies against the Guild key
    sig = doc["signature"]
    assert verify_jcs(ident, sig["signature"], sig["public_key"])
    assert sig["signer_did"] == store.guild_did()


def test_publish_gate_excludes_failing_capability():
    # a capability whose fixture suite fails must NOT get an identity
    import app.swarm.capabilities as caps_mod
    broken = caps_mod.Capability(
        id="test.broken", version="1.0.0", name="Broken", summary="s",
        description="d", tags=("t",),
        input_schema={"type": "object", "properties": {}, "required": [],
                      "additionalProperties": False},
        output_schema={"type": "object"},
        run=lambda p: {"x": 1},
        fixtures=({"input": {}, "expect_subset": {"x": 2}},),  # will fail
        failure_modes=("f",), prohibited_uses=("p",),
        demand_hypothesis="h")
    caps_mod.CAPABILITIES["test.broken"] = broken
    try:
        reg = IdentityRegistry()
        result = reg.build("http://test", store.guild_identity(), {})
        assert "test.broken" in result["excluded"]
        assert reg.for_capability("test.broken") is None
        assert result["published"] == len(caps_mod.CAPABILITIES) - 1
    finally:
        del caps_mod.CAPABILITIES["test.broken"]


def test_unknown_identity_404():
    assert client.get("/identities/agid_nope").status_code == 404


def test_capability_readiness_is_fresh_scoped_and_signed():
    response = client.get("/.well-known/ag-capability-readiness.json")
    assert response.status_code == 200
    envelope = response.json()
    body = envelope["readiness"]
    assert body["schema_version"] == "ag-capability-readiness/1"
    assert body["transport_state"]["state"] == "reachable"
    assert body["model_state"]["state"] == "not_applicable"
    assert body["admission_state"]["global_gate_state"] == "open"
    assert body["admission_state"]["caller_admission"] == "not_evaluated"
    assert body["capability_count"] == len(CAPABILITIES)
    record = body["capabilities"][0]
    for field in (
            "capability_id", "readiness_state", "dependency_set_digest",
            "last_terminal_observed_at", "last_terminal_outcome",
            "last_terminal_result_digest", "fresh_until"):
        assert field in record
    assert record["readiness_state"] == "ready"
    assert record["callability_state"] == "not_evaluated"
    assert record["dependency_set_digest"].startswith("sha256:")
    assert record["last_terminal_result_digest"].startswith("sha256:")
    assert "dependencies" not in record
    sig = envelope["signature"]
    assert verify_jcs(body, sig["signature"], sig["public_key"])
    assert sig["signer_did"] == store.guild_did()
    assert response.headers["cache-control"] == "public, max-age=30, s-maxage=30"


def test_single_capability_readiness_and_unknown_contract():
    capability_id = sorted(CAPABILITIES)[0]
    response = client.get(f"/capabilities/{capability_id}/readiness")
    assert response.status_code == 200
    body = response.json()["readiness"]
    assert body["capability_count"] == 1
    assert body["capabilities"][0]["capability_id"] == capability_id
    unknown = client.get("/capabilities/not.real/readiness")
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["error"] == "unknown capability"


def test_terminal_and_dependency_digests_are_stable_across_fresh_reads():
    capability_id = sorted(CAPABILITIES)[0]
    first = client.get(f"/capabilities/{capability_id}/readiness").json()
    second = client.get(f"/capabilities/{capability_id}/readiness").json()
    a = first["readiness"]["capabilities"][0]
    b = second["readiness"]["capabilities"][0]
    assert a["last_terminal_result_digest"] == b["last_terminal_result_digest"]
    assert a["dependency_set_digest"] == b["dependency_set_digest"]
    assert first["readiness"]["generated_at"] != second["readiness"]["generated_at"]


def test_failed_canary_never_claims_ready_or_callable():
    import app.swarm.capabilities as caps_mod
    broken = caps_mod.Capability(
        id="test.unready", version="1.0.0", name="Unready", summary="s",
        description="d", tags=("t",),
        input_schema={"type": "object", "properties": {}, "required": [],
                      "additionalProperties": False},
        output_schema={"type": "object"}, run=lambda payload: {"ok": False},
        fixtures=({"input": {}, "expect_subset": {"ok": True}},),
        failure_modes=("f",), prohibited_uses=("p",))
    caps_mod.CAPABILITIES[broken.id] = broken
    try:
        record = readiness.capability_record(broken)
        assert record["readiness_state"] == "failed"
        assert record["callability_state"] == "not_evaluated"
        assert record["last_terminal_outcome"] == "fixture_failure"
    finally:
        del caps_mod.CAPABILITIES[broken.id]


def test_admission_block_is_separate_from_terminal_readiness():
    cap = CAPABILITIES[sorted(CAPABILITIES)[0]]
    readiness.prime_cache(registry.gate_results())
    document = readiness.readiness_document(
        store.guild_identity(), capability_id=cap.id, global_gate_open=False)
    record = document["readiness"]["capabilities"][0]
    assert record["readiness_state"] == "ready"
    assert record["callability_state"] == "blocked_global_gate"
    assert document["readiness"]["admission_state"]["caller_admission"] == \
        "not_evaluated"


def test_missing_terminal_canary_fails_closed():
    import app.swarm.capabilities as caps_mod
    empty = caps_mod.Capability(
        id="test.no_canary", version="1.0.0", name="No canary", summary="s",
        description="d", tags=("t",),
        input_schema={"type": "object"}, output_schema={"type": "object"},
        run=lambda payload: {}, fixtures=(), failure_modes=("f",),
        prohibited_uses=("p",))
    caps_mod.CAPABILITIES[empty.id] = empty
    try:
        record = readiness.capability_record(empty)
        assert record["readiness_state"] == "unknown"
        assert record["last_terminal_outcome"] == "missing_canary"
        assert record["last_terminal_observed_at"] is None
        assert record["last_terminal_result_digest"] is None
        assert caps_mod.run_fixtures(empty)["ok"] is False
    finally:
        del caps_mod.CAPABILITIES[empty.id]


def test_output_schema_violation_fails_readiness_even_when_subset_matches():
    import app.swarm.capabilities as caps_mod
    invalid = caps_mod.Capability(
        id="test.invalid_output", version="1.0.0", name="Invalid", summary="s",
        description="d", tags=("t",),
        input_schema={"type": "object"},
        output_schema={"type": "object", "properties": {
            "value": {"type": "integer"}}, "required": ["value"],
            "additionalProperties": False},
        run=lambda payload: {"value": "wrong"},
        fixtures=({"input": {}, "expect_subset": {"value": "wrong"}},),
        failure_modes=("f",), prohibited_uses=("p",))
    caps_mod.CAPABILITIES[invalid.id] = invalid
    try:
        record = readiness.capability_record(invalid)
        assert record["readiness_state"] == "failed"
        result = caps_mod.run_fixtures(invalid)
        assert result["ok"] is False
        assert result["failures"][0]["reason"].startswith("output schema:")
    finally:
        del caps_mod.CAPABILITIES[invalid.id]


def test_public_readiness_uses_cache_and_does_not_persist_fetch_events(monkeypatch):
    readiness.prime_cache(registry.gate_results())
    before = len(store.events)

    def unexpected_canary(*args, **kwargs):
        raise AssertionError("fresh cache must not execute a public-request canary")

    monkeypatch.setattr(readiness, "capability_record", unexpected_canary)
    assert client.get("/.well-known/ag-capability-readiness.json").status_code == 200
    assert len(store.events) == before


def test_uncached_readiness_refresh_completes_in_isolated_worker():
    readiness.clear_cache_for_tests()
    document = readiness.readiness_document(store.guild_identity())
    body = document["readiness"]
    assert all(record["readiness_state"] == "ready"
               for record in body["capabilities"])
    generated = readiness._aware_timestamp(body["generated_at"])
    observed = [readiness._aware_timestamp(record["last_terminal_observed_at"])
                for record in body["capabilities"]]
    assert generated is not None
    assert all(item is not None and generated >= item for item in observed)
    readiness.prime_cache(registry.gate_results())


def test_malformed_success_worker_output_is_rejected_before_signing(monkeypatch):
    import json

    class MalformedWorker:
        returncode = 0

        def communicate(self, timeout=None):
            malformed = {cap_id: {"readiness_state": "ready"}
                         for cap_id in CAPABILITIES}
            return json.dumps(malformed), ""

    starts = 0

    def start_worker(*args, **kwargs):
        nonlocal starts
        starts += 1
        return MalformedWorker()

    readiness.clear_cache_for_tests()
    monkeypatch.setattr(readiness.subprocess, "Popen", start_worker)
    first = readiness.readiness_document(store.guild_identity())
    assert all(record["readiness_state"] == "unknown"
               for record in first["readiness"]["capabilities"])
    second = readiness.readiness_document(store.guild_identity())
    assert all(record["readiness_state"] == "unknown"
               for record in second["readiness"]["capabilities"])
    assert starts == 1
    readiness.prime_cache(registry.gate_results())


def test_refresh_timeout_is_bounded_and_fails_closed(monkeypatch):
    import subprocess
    import time

    class StuckWorker:
        returncode = None

        def __init__(self):
            self.communications = 0
            self.killed = False

        def communicate(self, timeout=None):
            self.communications += 1
            if self.communications == 1:
                raise subprocess.TimeoutExpired("canary", timeout)
            self.returncode = -9
            return "", ""

        def kill(self):
            self.killed = True

    worker = StuckWorker()
    starts = 0

    def start_worker(*args, **kwargs):
        nonlocal starts
        starts += 1
        return worker

    readiness.clear_cache_for_tests()
    monkeypatch.setattr(readiness, "REFRESH_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(readiness.subprocess, "Popen", start_worker)
    started = time.perf_counter()
    document = readiness.readiness_document(store.guild_identity())
    elapsed = time.perf_counter() - started
    assert elapsed < 0.04
    assert worker.killed is True
    assert worker.communications == 2
    assert all(r["readiness_state"] == "unknown"
               for r in document["readiness"]["capabilities"])
    started = time.perf_counter()
    readiness.readiness_document(store.guild_identity())
    assert time.perf_counter() - started < 0.02
    assert starts == 1
    readiness.prime_cache(registry.gate_results())


def test_swarm_identities_registered_as_first_party_supply():
    swarm_agents = [a for a in store.agents.values()
                    if (a.get("metadata") or {}).get("swarm_identity")]
    assert len(swarm_agents) == len(CAPABILITIES)
    assert all(a["first_party"] for a in swarm_agents)   # excluded from growth
    # idempotent: ensure_built again doesn't duplicate
    registry._built_at = None
    ensure_built()
    again = [a for a in store.agents.values()
             if (a.get("metadata") or {}).get("swarm_identity")]
    assert len(again) == len(CAPABILITIES)


def test_a2a_card_advertises_swarm_skills():
    card = client.get("/.well-known/agent-card.json").json()
    ids = {s["id"] for s in card["skills"]}
    assert "guild.invoke" in ids
    assert "ag.json.repair" in ids
    assert len(ids) >= len(CAPABILITIES) + 2


def test_manifest_links_swarm_surfaces():
    m = client.get("/.well-known/agent-guild.json").json()
    assert "invocable_capabilities" in m
    assert m["invocable_capabilities"]["index"] == "/.well-known/ag-identities/index.json"
    assert m["discovery"]["ag_identities"] == "/.well-known/ag-identities/index.json"


def test_llms_txt_mentions_guest_invocation():
    txt = client.get("/llms.txt").text
    assert "/invoke/" in txt and "ag-identities" in txt


def test_mcp_tools_registered_per_capability():
    import asyncio
    from app.mcp_server import mcp
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert "ag_capabilities" in names
    for cap_id in CAPABILITIES:
        assert "ag_" + cap_id.replace(".", "_") in names
