"""/release — non-secret release identity for the deployment-aware gate."""
import os

from fastapi.testclient import TestClient

from app import __version__
from app.main import app

client = TestClient(app)


def test_release_reports_version_sha_and_timestamp(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "a" * 40)
    r = client.get("/release").json()
    assert r["version"] == __version__
    assert r["git_sha"] == "a" * 40
    assert r["deployed_at"]          # ISO timestamp captured at process start


def test_release_sha_is_honestly_unknown_without_platform_env(monkeypatch):
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    monkeypatch.delenv("GUILD_GIT_SHA", raising=False)
    r = client.get("/release").json()
    # "unknown" can never equal a pushed SHA, so the release gate treats an
    # unidentified deployment as NOT verified — never as a pass
    assert r["git_sha"] == "unknown"


def test_release_exposes_no_secrets():
    body = client.get("/release").json()
    flat = str(body).lower()
    for needle in ("token", "secret", "key", "password"):
        assert needle not in flat
