"""The public machine-envelope receiver must be the reviewed standalone file."""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_served_receiver_is_reviewed_and_machine_discoverable():
    source = ROOT / "sdk" / "integrations" / "machine_envelope_receiver.mjs"
    served = (ROOT / "live" / "guild" / "app" / "artifacts" /
              "integrations" / "machine_envelope_receiver.mjs")
    assert source.read_bytes() == served.read_bytes()
    with TestClient(app) as client:
        response = client.get(
            "/sdk/integrations/machine_envelope_receiver.mjs")
        assert response.status_code == 200
        assert response.content == source.read_bytes()
        manifest = client.get("/.well-known/agent-guild.json").json()
        receiver = manifest["discovery"]["receiver_integrations"][
            "machine_envelope"]
        assert receiver["source"] == \
            "/sdk/integrations/machine_envelope_receiver.mjs"
        assert receiver["free_discovery"] is True
        assert receiver["a2a_extension"] == \
            "/extensions/machine-envelope/v1"
        assert receiver["activation_header"] == "A2A-Extensions"
        assert receiver["consequential_messages"] == \
            "paid envelope required"
        assert receiver["replay"] == "atomic consume before side effects"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_node_receiver_fails_closed_and_consumes_once():
    script = pathlib.Path(__file__).with_name(
        "node_machine_envelope_receiver_test.mjs")
    result = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "machine envelope receiver gate tests passed" in result.stdout
