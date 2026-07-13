"""One-counterparty binding (corrective pass 2026-07-13).

A machine consuming /check must never approve one identity, invoke another, or
attribute an outcome to a third. These tests pin the invariant:

  routing.routable == true  =>  decision.agent_id == routing.provider_id
                                decision.identity.did == routing.provider_did
                                decision.endpoint(+sha256) == routed endpoint
                                capability/evidence concern that same provider

covering the exact live failure: the evidence-ranked #1 has NO verified
reachable endpoint, while a lower-ranked provider is routable.
"""
from __future__ import annotations

import json
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app, store  # noqa: E402
from app import reachability as R  # noqa: E402

client = TestClient(app)

_N = [0]


def _register(name, cap, endpoint=None):
    meta = {"endpoint": endpoint} if endpoint else {}
    return client.post("/agents/register",
                       json={"name": name, "capabilities": [cap],
                             "metadata": meta}).json()


def _earn(worker_id, cap, n, rating=0.9):
    r = client.post("/agents/register",
                    json={"name": f"req-for-{worker_id}-{n}",
                          "capabilities": []}).json()
    for i in range(n):
        client.post("/collaborations", headers={"X-API-Key": r["api_key"]},
                    json={"worker_id": worker_id, "capability": cap,
                          "outcome": "accepted", "rating": rating,
                          "deliverable": f"d{i}"})


def _make_routable(agent_id, endpoint):
    """Stamp a fresh invocation-verified reachability record (the internal
    trusted path — equivalent to a Guild-originated bound invocation)."""
    agent = store.get_agent(agent_id)
    agent["metadata"]["endpoint"] = endpoint
    agent["reachability"] = R.invocation_verified_record(endpoint, "inv_test")


def _setup():
    # top: strictly more earned evidence (higher trust), endpoint DECLARED but
    # never verified -> not routable. lower: less evidence, but routable.
    # Each setup uses a UNIQUE capability so tests cannot cross-contaminate.
    _N[0] += 1
    cap = f"bind-cap-{_N[0]}"
    top = _register("bind-top", cap, endpoint="https://top.example/a2a")
    lower = _register("bind-lower", cap, endpoint="https://lower.example/a2a")
    _earn(top["id"], cap, 6)
    _earn(lower["id"], cap, 2)
    _make_routable(lower["id"], "https://lower.example/a2a")
    return top, lower, cap


def test_highest_ranked_unreachable_lower_ranked_routable():
    top, lower, cap = _setup()
    out = client.get("/check", params={"capability": cap}).json()
    routing, decision = out["routing"], out["decision"]
    assert routing["routable"] is True
    assert routing["provider_id"] == lower["id"]
    # the decision used for routing is ABOUT the routed provider
    assert decision["agent_id"] == routing["provider_id"]
    assert decision["identity"]["did"] == routing["provider_did"]
    assert decision["endpoint"] == routing["endpoint"]
    assert decision["endpoint_sha256"] == routing["endpoint_sha256"]
    assert decision["endpoint_sha256"] is not None
    assert decision["capability_match"]["requested"] == cap
    assert lower["id"] in json.dumps(decision["evidence_provenance"]) or \
        decision["agent_id"] == lower["id"]
    # legacy presentation follows the SAME counterparty
    assert out["best_agent"]["id"] == lower["id"]
    assert out["verdict"]["agent_id"] == lower["id"]
    # the evidence-top is exposed only as a non-actionable object
    hr = out["highest_ranked"]
    assert hr["agent_id"] == top["id"]
    assert hr["actionable"] is False
    assert "endpoint" not in hr and "did" not in hr


def test_signed_envelope_carries_the_same_binding():
    top, lower, cap = _setup()
    sd = client.get("/check", params={"capability": cap,
                                      "signed": "true"}).json()
    d, r = sd["decision"], sd["routing"]
    assert r["routable"] is True
    assert d["agent_id"] == r["provider_id"] == lower["id"]
    assert d["identity"]["did"] == r["provider_did"]
    assert d["endpoint_sha256"] == r["endpoint_sha256"]
    # value tier + evidence concern the routed provider (committed evidence)
    assert d["evidence_provenance"]["verifiable_collaborations"] >= 1


def test_not_routable_when_no_verified_endpoint_anywhere():
    # fresh capability with only a declared-unverified supplier
    a = client.post("/agents/register",
                    json={"name": "bind-solo",
                          "capabilities": ["bind-solo-cap"],
                          "metadata": {"endpoint": "https://solo.example"}}
                    ).json()
    _r = client.post("/agents/register",
                     json={"name": "solo-req", "capabilities": []}).json()
    client.post("/collaborations", headers={"X-API-Key": _r["api_key"]},
                json={"worker_id": a["id"], "capability": "bind-solo-cap",
                      "outcome": "accepted", "rating": 0.9,
                      "deliverable": "d"})
    out = client.get("/check", params={"capability": "bind-solo-cap"}).json()
    assert out["routing"]["routable"] is False
    assert out["routing"]["provider_id"] is None
    # non-routable: the evaluated provider is the evidence-top, and no
    # highest_ranked split exists
    assert out["decision"]["agent_id"] == a["id"]
    assert "highest_ranked" not in out


def test_binding_mismatch_fails_closed():
    """If the internal invariant were ever violated, /check must close the
    routing gate rather than serve a mismatched identity."""
    _top, lower, cap = _setup()
    original = store.risk_for
    try:
        # sabotage: make the decision unbuildable for the routed provider
        store.risk_for = lambda agent_id: None
        out = client.get("/check", params={"capability": cap}).json()
        assert out["routing"]["routable"] is False
        assert "binding" in out["routing"]["reason"] or \
            out["routing"]["provider_id"] is None
    finally:
        store.risk_for = original
