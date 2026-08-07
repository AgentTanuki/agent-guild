"""The public x402 hook must be standalone, exact and fail closed."""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_served_x402_policy_is_the_reviewed_source():
    source = ROOT / "sdk" / "integrations" / "x402_payment_policy.mjs"
    served = (ROOT / "live" / "guild" / "app" / "artifacts" /
              "integrations" / "x402_payment_policy.mjs")
    assert source.read_bytes() == served.read_bytes()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_node_x402_policy_verifies_before_signing_and_fails_closed():
    script = pathlib.Path(__file__).with_name(
        "node_x402_payment_policy_test.mjs")
    result = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "signed allow, tamper, unpaid, local cap, recursion guard ok" \
        in result.stdout
