"""The PayanAgent MCP adapter is single-file, protected and fail closed."""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_served_payanagent_policy_is_the_reviewed_bundle():
    source = ROOT / "sdk" / "integrations" / "payanagent_payment_policy.mjs"
    served = (ROOT / "live" / "guild" / "app" / "artifacts" /
              "integrations" / "payanagent_payment_policy.mjs")
    assert source.read_bytes() == served.read_bytes()
    text = source.read_text()
    assert "createPaymentPolicy" in text
    assert "createAgentGuildX402PaymentPolicy" in text
    assert 'DEFAULT_MODE = "protected"' in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_node_payanagent_policy_defaults_protected_and_fails_closed():
    script = pathlib.Path(__file__).with_name(
        "node_payanagent_payment_policy_test.mjs")
    result = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "protected default, unpaid, cap, standard, validation ok" in result.stdout
