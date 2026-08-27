"""Focused contracts for vendor-neutral machine discovery surfaces."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi.testclient import TestClient

from app import x402
from app.crypto import generate_keypair
from app.main import app


UNTRUSTED_REQUEST_BASE = "https://attacker.example"
PUBLIC_BASE = x402.public_host()
client = TestClient(app, base_url=UNTRUSTED_REQUEST_BASE)


def test_agents_txt_is_plain_text_with_an_exact_first_action():
    response = client.get("/agents.txt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert f"MCP-Endpoint: {PUBLIC_BASE}/mcp" in response.text
    assert (
        f"MCP-Server-Card: {PUBLIC_BASE}/.well-known/mcp/server-card.json"
        in response.text
    )
    assert "First-Tool: guild_preflight" in response.text
    assert (
        'First-Arguments: {"url":"<absolute counterparty endpoint URL>"}'
        in response.text
    )
    assert f"Full-Agent-Guide: {PUBLIC_BASE}/agents.md" in response.text
    assert "attacker.example" not in response.text


def test_rfc9727_get_is_profiled_json_linkset_from_trusted_public_base():
    response = client.get("/.well-known/api-catalog")

    assert response.status_code == 200
    content_type = response.headers["content-type"]
    assert content_type.startswith("application/linkset+json")
    assert 'profile="https://www.rfc-editor.org/info/rfc9727"' in content_type
    assert response.headers["link"] == (
        f'<{PUBLIC_BASE}/.well-known/api-catalog>; rel="api-catalog"; '
        'type="application/linkset+json"'
    )

    linkset = response.json()["linkset"]
    assert len(linkset) == 1
    assert linkset[0]["anchor"] == PUBLIC_BASE
    endpoints = {link["href"] for link in linkset[0]["item"]}
    assert endpoints == {
        PUBLIC_BASE,
        f"{PUBLIC_BASE}/mcp",
        f"{PUBLIC_BASE}/a2a",
    }
    assert all(urlparse(href).scheme == "https" for href in endpoints)
    assert all(
        urlparse(href).netloc == urlparse(PUBLIC_BASE).netloc
        for href in endpoints
    )
    links = linkset[0]["service-desc"] + linkset[0]["service-meta"]
    hrefs = {link["href"] for link in links}
    assert hrefs == {
        f"{PUBLIC_BASE}/openapi.json",
        f"{PUBLIC_BASE}/.well-known/mcp/server-card.json",
        f"{PUBLIC_BASE}/.well-known/agent-card.json",
        f"{PUBLIC_BASE}/llms.txt",
        f"{PUBLIC_BASE}/.well-known/agent-guild.json",
    }
    assert all(urlparse(href).scheme == "https" for href in hrefs)
    assert all(
        urlparse(href).netloc == urlparse(PUBLIC_BASE).netloc
        for href in hrefs
    )
    assert "attacker.example" not in response.text


def test_rfc9727_head_advertises_the_catalog_relation_without_a_body():
    response = client.head("/.well-known/api-catalog")

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-type"].startswith(
        "application/linkset+json"
    )
    assert response.headers["link"] == (
        f'<{PUBLIC_BASE}/.well-known/api-catalog>; rel="api-catalog"; '
        'type="application/linkset+json"'
    )


def test_auth_md_states_real_admission_and_no_delegated_oauth():
    response = client.get("/auth.md")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert f"POST {PUBLIC_BASE}/agents/register" in response.text
    assert f"POST {PUBLIC_BASE}/billing/trial" in response.text
    assert "without a public_key" in response.text
    assert "custodial=false and api_key=null" in response.text
    assert "X-API-Key" in response.text
    assert "x402" in response.text
    assert "eip155:8453" in response.text
    assert "Some other reads are metered" in response.text
    assert "Agent Guild uses no delegated OAuth" in response.text
    assert "delegated OAuth token" in response.text
    assert "attacker.example" not in response.text


def test_self_sovereign_registration_does_not_claim_or_return_an_api_key():
    _, public_key = generate_keypair()
    response = client.post(
        "/agents/register",
        json={
            "name": "machine-discovery-self-sovereign-test",
            "capabilities": ["contract-testing"],
            "public_key": public_key,
        },
    )

    assert response.status_code == 200, response.text
    registration = response.json()
    assert registration["public_key"] == public_key
    assert registration["custodial"] is False
    assert registration["api_key"] is None


def test_security_txt_has_rfc9116_required_fields_and_https_canonical():
    response = client.get("/.well-known/security.txt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "text/plain; charset=utf-8"
    )
    fields = [line for line in response.text.splitlines() if line]
    assert fields.count("Expires: 2027-05-01T00:00:00Z") == 1
    assert (
        "Contact: https://github.com/AgentTanuki/agent-guild/security/"
        "advisories/new"
    ) in fields
    assert (
        "Policy: https://github.com/AgentTanuki/agent-guild/security/policy"
        in fields
    )
    canonical = [line for line in fields if line.startswith("Canonical: ")]
    assert canonical == [
        f"Canonical: {PUBLIC_BASE}/.well-known/security.txt"
    ]
    assert urlparse(canonical[0].removeprefix("Canonical: ")).scheme == "https"
    assert "attacker.example" not in response.text

    expires = datetime.fromisoformat(
        fields[fields.index("Expires: 2027-05-01T00:00:00Z")]
        .removeprefix("Expires: ")
        .replace("Z", "+00:00")
    )
    reference = datetime(2026, 8, 27, tzinfo=timezone.utc)
    assert reference < expires < reference.replace(year=2027)
