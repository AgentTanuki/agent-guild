"""Agent Identity & Discovery (AID) v2 HTTPS fallback."""
from urllib.parse import urlparse

from fastapi.testclient import TestClient

from app import main, x402


def test_aid_v2_well_known_fallback_is_valid_and_cacheable():
    client = TestClient(main.app)
    response = client.get(
        "/.well-known/agent",
        headers={"user-agent": "aid-python/2.1"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == (
        "public, max-age=300, s-maxage=300")
    assert len(response.content) < 64 * 1024

    base = x402.public_host().rstrip("/")
    assert response.json() == {
        "v": "aid2",
        "u": f"{base}/mcp",
        "p": "mcp",
        "a": "none",
        "s": "Agent Guild trust and settlement",
        "d": f"{base}/for-agents",
    }
    assert urlparse(response.json()["u"]).scheme == "https"
    assert len(response.json()["s"].encode("utf-8")) <= 60


def test_aid_fetch_is_one_noncommercial_discovery_observation():
    client = TestClient(main.app)
    before = len(main.store.events)

    response = client.get(
        "/.well-known/agent",
        headers={"user-agent": "aid-python/2.1"},
    )

    assert response.status_code == 200
    new = main.store.events[before:]
    observations = [event for event in new
                    if event["type"] == "discovery_resource_fetched"]
    assert len(observations) == 1
    assert observations[0]["discovery_surface"] == "aid_v2"
    assert not any(event["type"] in {
        "capability_demand", "offer_served", "paid_offer_served",
        "paid_offer_shown",
    } for event in new)
