"""Gateway end-to-end against the real local Guild: gate → report → the
outcome lands in the Guild ledger (evidence completion is automatic)."""
from __future__ import annotations

import json
import urllib.request


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=15) as r:
        return json.loads(r.read().decode())


def test_gate_allows_micro_and_reports_outcome(guild_server, seeded, gateway):
    gateway.client.api_key = seeded["requester"]["api_key"]
    gate = gateway.gate("tp-echo", value_at_risk=1.0)
    assert gate.allowed and gate.channel == "live"
    assert gate.worker_id == seeded["worker"]["id"]
    before = len(guild_server["store"].tasks)
    rec = gateway.report(gate, "accepted", deliverable="result-bytes",
                         latency_ms=12.0)
    assert rec["signature"]
    assert len(guild_server["store"].tasks) > before   # evidence landed
    snap = gateway.snapshot()
    assert snap["outcomes"]["flushed"] >= 1


def test_gate_denies_high_tier_and_records_block(guild_server, seeded, gateway):
    gate = gateway.gate("tp-echo", value_at_risk=5000.0)
    assert not gate.allowed
    gateway.report(gate, "blocked")
    assert gateway.snapshot()["denied"] >= 1


def test_gateway_outage_uses_cache_then_fail_mode(guild_server, seeded, tmp_path):
    from agentguild_trustplane.gateway import Gateway
    from agentguild_trustplane.policy import RiskPolicy
    gw = Gateway(policy=RiskPolicy(), state_dir=tmp_path / "s1",
                 base_url=guild_server["base"])
    assert gw.gate("tp-echo", 1.0).channel == "live"     # warms the cache
    gw.client.base = "http://127.0.0.1:9"                # simulate outage
    gw.client.timeout = 0.5
    g2 = gw.gate("tp-echo", 1.0)
    assert g2.channel == "cache" and g2.allowed          # signed cache serves
    g3 = gw.gate("never-seen-capability", 1.0)
    assert g3.channel == "outage"
    assert g3.allowed                                    # micro fail_mode=open
    g4 = gw.gate("never-seen-capability", 5000.0)
    assert not g4.allowed                                # high fail_mode=closed
    m = gw.snapshot()
    assert m["cache"]["hit_fresh"] >= 1 and m["outage_gates"] >= 2


def test_sidecar_http_surface(guild_server, seeded, gateway):
    from fastapi.testclient import TestClient
    from agentguild_trustplane.sidecar import build_app
    gateway.client.api_key = seeded["requester"]["api_key"]
    c = TestClient(build_app(gateway))
    g = c.post("/gate", json={"capability": "tp-echo", "value_at_risk": 1}).json()
    assert g["allowed"] and g["policy"]["policy_id"] == "default"
    r = c.post("/report", json={"gate_id": g["gate_id"], "capability": "tp-echo",
                                "outcome": "accepted", "deliverable": "x"})
    assert r.status_code == 200 and r.json()["signature"]
    m = c.get("/metrics").json()
    assert m["gates"] >= 1
    assert c.get("/policy").json()["policy_id"] == "default"
