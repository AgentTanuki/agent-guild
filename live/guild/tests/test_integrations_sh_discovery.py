"""integrations.sh owner declaration is complete, honest, and crawler-safe."""
from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)
BASE = "https://agent-guild-5d5r.onrender.com"
DECLARATION = f"{BASE}/.well-known/integrations.json"


def test_integrations_owner_declaration_is_public_and_cacheable():
    response = client.get(
        "/.well-known/integrations.json",
        headers={"Origin": "https://integrations.sh"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["cache-control"] == (
        "public, max-age=300, s-maxage=300"
    )
    assert "/.well-known/integrations.json" in (
        client.get("/openapi.json").json()["paths"]
    )


def test_integrations_owner_declaration_names_only_real_surfaces():
    body = client.get("/.well-known/integrations.json").json()

    assert body["version"] == 3
    surfaces = {surface["slug"]: surface for surface in body["surfaces"]}
    assert set(surfaces) == {"agent-guild-mcp", "agent-guild-http-api"}

    mcp = surfaces["agent-guild-mcp"]
    assert mcp["type"] == "mcp"
    assert mcp["url"] == f"{BASE}/mcp/"
    assert mcp["transports"] == ["streamable-http"]
    assert mcp["auth"]["status"] == "none"

    http = surfaces["agent-guild-http-api"]
    assert http["type"] == "http"
    assert http["url"] == BASE
    assert http["spec"] == f"{BASE}/openapi.json"
    # The API mixes free and metered operations; a single required/none claim
    # would be false at surface level.
    assert http["auth"] == {"status": "unknown"}

    for surface in surfaces.values():
        assert surface["docs"] == f"{BASE}/for-agents"
        assert surface["basis"] == {
            "via": "declared",
            "source": DECLARATION,
        }
    assert mcp["auth"]["basis"] == {
        "via": "declared",
        "source": DECLARATION,
    }
