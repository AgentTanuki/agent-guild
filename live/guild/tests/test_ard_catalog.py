"""ARD catalogue: pinned-schema conformance and truthful local references."""
from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys
from urllib.parse import urlparse

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from app.main import app
from app.state import store


HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[2]
SCHEMA_PATH = (REPO / "live" / "third_party" / "ard-spec" / "spec" /
               "schemas" / "ai-catalog.schema.json")
SCHEMA_SHA256 = "c55238483a4738e08b250bdd6af1f4dc05a91afe882c649d224d09c19cd8fe09"
CLI_SHA256 = "fa387310d5f28358012ecb676b8257ef41e6015ea29905879e6802e0cb7df6b4"
COMMERCIAL_EVENTS = {
    "capability_demand", "offer_served", "paid_offer_served",
    "paid_offer_shown",
}
client = TestClient(app)


def _commercial_events():
    return [e for e in store.events if e.get("type") in COMMERCIAL_EVENTS]


def test_catalog_matches_the_exact_pinned_ard_schema_without_attribution():
    assert hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest() == SCHEMA_SHA256
    schema = json.loads(SCHEMA_PATH.read_text())
    before = list(_commercial_events())

    response = client.get(
        "/.well-known/ai-catalog.json",
        headers={"Origin": "https://crawler.example"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["cache-control"] == (
        "public, max-age=300, s-maxage=300"
    )
    manifest = response.json()
    Draft202012Validator(schema).validate(manifest)
    assert "$schema" not in manifest
    assert _commercial_events() == before


def test_catalog_entries_are_unique_queryable_https_artifacts_that_resolve():
    manifest = client.get("/.well-known/ai-catalog.json").json()
    entries = manifest["entries"]
    identifiers = [entry["identifier"] for entry in entries]
    assert len(identifiers) == len(set(identifiers))
    assert {entry["type"] for entry in entries} == {
        "application/mcp-server-card+json",
        "application/a2a-agent-card+json",
        "application/ai-skill+md",
    }

    for entry in entries:
        assert 2 <= len(entry["representativeQueries"]) <= 5
        parsed = urlparse(entry["url"])
        assert parsed.scheme == "https"
        assert parsed.netloc == "agent-guild-5d5r.onrender.com"
        artifact = client.get(parsed.path)
        assert artifact.status_code == 200, entry["identifier"]
        if entry["type"].endswith("+json"):
            assert artifact.headers["content-type"].startswith(
                "application/json")
        else:
            assert artifact.headers["content-type"].startswith("text/markdown")


def test_pinned_official_ard_conformance_cli_accepts_catalog(tmp_path):
    manifest = client.get("/.well-known/ai-catalog.json").json()
    path = tmp_path / "ai-catalog.json"
    path.write_text(json.dumps(manifest))
    cli = REPO / "live" / "third_party" / "ard-spec" / \
        "conformance" / "bin" / "conformance-test"
    assert hashlib.sha256(cli.read_bytes()).hexdigest() == CLI_SHA256
    result = subprocess.run(
        [sys.executable, str(cli), "manifest", str(path)],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
