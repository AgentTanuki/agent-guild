"""The public Taskmarket requester adapter must be served byte-for-byte."""
from __future__ import annotations

import pathlib
import os
import shutil
import subprocess

import pytest

os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_served_taskmarket_requester_is_the_reviewed_source():
    source = ROOT / "sdk" / "integrations" / "taskmarket_requester.mjs"
    served = (ROOT / "live" / "guild" / "app" / "artifacts" /
              "integrations" / "taskmarket_requester.mjs")
    assert source.read_bytes() == served.read_bytes()
    with TestClient(app) as client:
        response = client.get("/sdk/integrations/taskmarket_requester.mjs")
        assert response.status_code == 200
        assert response.content == source.read_bytes()
        manifest = client.get("/.well-known/agent-guild.json").json()
        taskmarket = manifest["discovery"]["payment_policy_integrations"][
            "taskmarket"]
        assert taskmarket["source"] == \
            "/sdk/integrations/taskmarket_requester.mjs"
        assert "fresh approval" in taskmarket["note"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_node_taskmarket_requester_is_non_custodial_and_fail_closed():
    script = pathlib.Path(__file__).with_name(
        "node_taskmarket_requester_test.mjs")
    result = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "fresh approval, signed intent, exact cap, review and no-blind-retry paths ok" \
        in result.stdout
