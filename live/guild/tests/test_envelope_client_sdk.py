"""The public Node buyer must create exact caller proofs and verify issuance."""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_served_buyer_is_the_reviewed_source():
    source = ROOT / "sdk" / "agentguild_envelope_client.mjs"
    served = ROOT / "live" / "guild" / "app" / "artifacts" / source.name
    assert source.read_bytes() == served.read_bytes()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_node_buyer_builds_proof_hashes_payload_and_verifies_result():
    script = pathlib.Path(__file__).with_name("node_envelope_client_test.mjs")
    out = subprocess.run(
        ["node", str(script)], check=True, capture_output=True, text=True)
    assert "agentguild_envelope_client: ok" in out.stdout
