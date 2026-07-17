"""Verified machine demand must not depend on the User-Agent heuristic (P1).

A valid caller proof establishes cryptographically verified MACHINE IDENTITY
for a demand ask — a generic User-Agent (curl, empty, python-requests) can no
longer hide it. The UA heuristic remains only for UNPROVEN asks. Known
first-party DIDs stay excluded. "Verified machine identity" stays separate
from "verified external ownership" (the demand feed labels the former, never
the latter).
"""
import base64
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import callerproof, crypto, demand
from app.state import store
from app.swarm import runner

EXT_UA = "external-agent-framework/2.0 (crewai)"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GUILD_SCOUT_AUTORUN", "1")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "e" * 40)
    store.swarm_state.pop(runner.RUNNER_STATE_KEY, None)
    events_before = len(store.events)
    dedupe_before = dict(store.demand_dedupe)
    yield
    store.swarm_state.pop(runner.RUNNER_STATE_KEY, None)
    # verified demand rows sort to the TOP of the feed and (unsupplied)
    # never leave it — remove this module's residue so other modules'
    # capabilities are not crowded out of the scout's per-run cap
    del store.events[events_before:]
    store.demand_dedupe.clear()
    store.demand_dedupe.update(dedupe_before)


def _cap():
    return "uaind-" + uuid.uuid4().hex[:8]


def _did():
    priv, pub = crypto.generate_keypair()
    return priv, crypto.did_from_public_key(pub)


def _proof_header(priv, did, resource):
    env = callerproof.create_proof(priv, did, method="GET",
                                   resource=resource, body=b"")
    return base64.b64encode(json.dumps(env).encode()).decode()


def _row(cap):
    return next((r for r in store.demand_feed_entries()
                 if r["capability"] == cap), None)


@pytest.mark.parametrize("ua", ["curl/8.5.0", "", "python-requests/2.31"])
def test_valid_proof_with_generic_ua_counts_and_wakes(ua):
    from app.main import app
    cap = _cap()
    priv, did = _did()
    resource = f"/check?capability={cap}"
    with TestClient(app) as client:
        r = client.get(resource, headers={
            "User-Agent": ua,
            callerproof.HTTP_HEADER: _proof_header(priv, did, resource)})
        assert r.status_code in (200, 402, 404)
    row = _row(cap)
    assert row is not None
    assert row["verified_lookups"] == 1, (
        f"UA {ua!r} + VALID caller proof must count as verified machine "
        "demand — identity is proven by cryptography, not the UA string")
    assert row["genuine_lookups"] == 1
    assert cap.lower() in runner.pending_demand(store), (
        "cryptographically verified unmet demand must wake the scout")


@pytest.mark.parametrize("ua", ["curl/8.5.0", "", "python-requests/2.31"])
def test_invalid_proof_with_generic_ua_never_counts(ua):
    from app.main import app
    cap = _cap()
    priv, did = _did()
    other_priv, other_pub = crypto.generate_keypair()
    resource = f"/check?capability={cap}"
    env = callerproof.create_proof(priv, did, method="GET",
                                   resource="/check?capability=DIFFERENT",
                                   body=b"")
    with TestClient(app) as client:
        client.get(resource, headers={
            "User-Agent": ua,
            callerproof.HTTP_HEADER:
                base64.b64encode(json.dumps(env).encode()).decode()})
    row = _row(cap)
    assert row is not None
    assert row["verified_lookups"] == 0, (
        "an INVALID proof must never create verified machine demand")
    assert row["genuine_lookups"] == 0, (
        f"UA {ua!r} without a valid proof stays non-genuine (heuristic)")
    assert cap.lower() not in runner.pending_demand(store)


def test_first_party_did_remains_excluded():
    from app.main import app
    cap = _cap()
    priv, pub = crypto.generate_keypair()
    agent = store.register_agent(name="fp-demand-agent",
                                 capabilities=["ops"], metadata={},
                                 public_key=pub, first_party=True)
    did = agent["did"]
    resource = f"/check?capability={cap}"
    with TestClient(app) as client:
        client.get(resource, headers={
            "User-Agent": "curl/8.5.0",
            callerproof.HTTP_HEADER: _proof_header(priv, did, resource)})
    row = _row(cap)
    assert row is None or row["verified_lookups"] == 0, (
        "a KNOWN first-party DID must stay excluded from demand, however "
        "valid its proof")
    assert cap.lower() not in runner.pending_demand(store)


def test_framework_ua_without_proof_stays_heuristic_only():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        client.get(f"/check?capability={cap}",
                   headers={"User-Agent": EXT_UA})
    row = _row(cap)
    assert row is not None
    assert row["heuristic_lookups"] == 1
    assert row["verified_lookups"] == 0, (
        "a framework UA is a heuristic — it must never read as verified "
        "machine identity")


def test_feed_separates_identity_from_ownership():
    """The public feed may label VERIFIED MACHINE identity; it must not
    claim verified EXTERNAL ownership anywhere."""
    from app.main import app
    cap = _cap()
    priv, did = _did()
    resource = f"/check?capability={cap}"
    with TestClient(app) as client:
        client.get(resource, headers={
            "User-Agent": "curl/8.5.0",
            callerproof.HTTP_HEADER: _proof_header(priv, did, resource)})
        feed = client.get("/demand/feed").json()
    entry = next(e for e in feed["entries"] if e["capability"] == cap)
    assert entry["verified_lookups"] == 1
    assert "verified_external" not in json.dumps(entry), (
        "identity ≠ ownership: the feed must never call proven identity "
        "verified external")
