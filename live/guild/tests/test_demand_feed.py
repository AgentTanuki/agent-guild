"""B2 — the signed supplier-facing demand feed (/demand/feed).

A supplier machine must be able to: discover the feed from any published
machine surface, conditionally fetch it (ETag), verify its signature against
the Guild's did:web service key, and walk feed → register → prove → declare
endpoint without a human. The feed carries REAL unmet demand only (genuine
external asks, no verified-reachable supply), aggregates only (no actor ids,
no raw IPs, no prompts), and never the paid shortlist/scores/evidence.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import demand, x402
from app import x402_artifacts as artifacts
from app.crypto import canonicalize_jcs
from app.state import store

PAY_TO = "0x" + "11" * 20
EXT_UA = "external-agent-framework/2.0 (crewai)"


@pytest.fixture(autouse=True)
def _enforced(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    yield


def _cap():
    return "feed-cap-" + uuid.uuid4().hex[:8]


def _ask(client, cap, ua=EXT_UA):
    return client.get(f"/check?capability={cap}", headers={"User-Agent": ua})


def test_feed_lists_real_unmet_demand_with_counts_and_recency():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        assert _ask(client, cap).status_code == 402
        r = client.get("/demand/feed")
        assert r.status_code == 200
        body = r.json()
        assert body["schema"] == "agent-guild/demand-feed"
        assert body["feed_version"] == 1
        entry = next(e for e in body["entries"] if e["capability"] == cap)
        assert entry["demand_id"] == demand.demand_id_for(cap)
        assert entry["genuine_lookups"] == 1
        assert entry["verified_reachable"] == 0
        assert entry["first_seen"] and entry["last_seen"]
        assert "http" in entry["transports"]
        # the supplier path is complete and machine-executable
        sp = body["supplier_path"]
        assert sp["register"]["path"] == "/agents/register"
        assert sp["prove_identity"]["path"] == "/prove"
        assert sp["declare_endpoint"]["path"] == "/agents/{id}/endpoint"


def test_feed_signature_verifies_against_the_did_web_service_key():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        _ask(client, cap)
        body = client.get("/demand/feed").json()
        integrity = body.pop("integrity")
        # content hash binds the signature to the exact page content
        recomputed = artifacts.sha256_hex(
            canonicalize_jcs(body).encode("utf-8"))
        assert integrity["content_sha256"] == recomputed
        gid = store.guild_identity()
        assert integrity["kid"] == artifacts.kid_for_identity(gid)
        assert integrity["kid"].startswith("did:web:")
        payload = artifacts.jws_verify(integrity["jws"], gid["public_key"])
        assert payload and payload["content_sha256"] == recomputed
        assert payload["feed"] == "agent-guild/demand-feed"
        # tamper → the hash no longer matches
        body["entries"] = []
        assert artifacts.sha256_hex(
            canonicalize_jcs(body).encode("utf-8")) != recomputed


def test_feed_etag_conditional_fetch_and_pagination():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        _ask(client, cap)
        r1 = client.get("/demand/feed")
        etag = r1.headers["ETag"]
        assert etag
        r2 = client.get("/demand/feed", headers={"If-None-Match": etag})
        assert r2.status_code == 304
        # new demand changes the content → new ETag
        _ask(client, _cap())
        r3 = client.get("/demand/feed", headers={"If-None-Match": etag})
        assert r3.status_code == 200 and r3.headers["ETag"] != etag
        # pagination is explicit and bounded
        rp = client.get("/demand/feed?page=2&per_page=1")
        assert rp.status_code == 200
        pb = rp.json()
        assert pb["page"] == 2 and pb["per_page"] == 1
        assert len(pb["entries"]) <= 1
        assert client.get("/demand/feed?per_page=9999").status_code == 422


def test_feed_contains_no_actor_ids_ips_prompts_or_paid_payload():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        _ask(client, cap)
        blob = client.get("/demand/feed").text
        assert "actor" not in json.loads(blob)["entries"][0]
        assert "http:" not in blob.replace("http://", "")  # no hashed actors
        assert EXT_UA not in blob                           # no raw UAs
        assert "127.0.0.1" not in blob and "testclient" not in blob
        assert "shortlist" not in blob and '"estimate"' not in blob
        assert "attestations" not in blob


def test_feed_excludes_supplied_capabilities_and_non_genuine_demand():
    from app.main import app
    cap_supplied = _cap()
    cap_crawler = _cap()
    with TestClient(app) as client:
        # supplied on paper → not unmet → absent
        store.register_agent(name="s-" + cap_supplied,
                             capabilities=[cap_supplied], metadata={})
        _ask(client, cap_supplied)
        # crawler-only demand → not genuine → absent
        _ask(client, cap_crawler, ua="Glama-Bot/2.0 (+crawler)")
        caps = [e["capability"]
                for e in client.get("/demand/feed").json()["entries"]]
        assert cap_supplied not in caps
        assert cap_crawler not in caps


def test_feed_is_linked_from_every_machine_surface():
    from app.main import app
    import pathlib
    with TestClient(app) as client:
        manifest = client.get("/.well-known/agent-guild.json").json()
        assert manifest["discovery"]["demand_feed"]["path"] == "/demand/feed"
        assert client.get("/capabilities").json()["demand_feed"] == \
            "/demand/feed"
        assert "/demand/feed" in client.get("/llms.txt").text
        card = client.get("/.well-known/agent-card.json").json()
        exts = card["capabilities"]["extensions"]
        trust = next(e for e in exts
                     if "trust" in e["uri"])
        assert trust["params"]["demand_feed"].endswith("/demand/feed")
    server = json.loads(
        (pathlib.Path(__file__).resolve().parents[3] / "server.json")
        .read_text())
    meta = server["_meta"][
        "io.modelcontextprotocol.registry/publisher-provided"]
    assert meta["ai.agent-guild/trust"]["demand_feed"].endswith("/demand/feed")


def test_cold_discovery_path_registry_to_supplier_actions():
    """Acceptance gate: a clean client with NO preloaded AG hostname starts
    from the registry artifact (server.json — what the MCP Registry serves),
    finds the host, the demand feed, and the complete supplier path."""
    import pathlib
    from app.main import app
    server = json.loads(
        (pathlib.Path(__file__).resolve().parents[3] / "server.json")
        .read_text())
    host = server["websiteUrl"]                    # discovered, not preloaded
    assert host.startswith("https://")
    feed_url = server["_meta"][
        "io.modelcontextprotocol.registry/publisher-provided"][
        "ai.agent-guild/trust"]["demand_feed"]
    assert feed_url == host + "/demand/feed"
    # the same path served by the app (TestClient stands in for the host)
    cap = _cap()
    with TestClient(app) as client:
        _ask(client, cap)
        feed = client.get("/demand/feed").json()
        entry = next(e for e in feed["entries"] if e["capability"] == cap)
        # register against the demand — the exact action the feed names
        reg = client.post(feed["supplier_path"]["register"]["path"],
                          json={"name": "cold-supplier",
                                "capabilities": [entry["capability"]]})
        assert reg.status_code == 200
        assert reg.json()["id"]
        # once supplied, the entry leaves the unmet feed
        caps = [e["capability"]
                for e in client.get("/demand/feed").json()["entries"]]
        assert cap not in caps
