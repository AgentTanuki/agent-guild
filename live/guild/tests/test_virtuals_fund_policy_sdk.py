"""The Virtuals ACP adapter must fail closed and verify wallet evidence."""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest


def test_node_virtuals_fund_policy_adapter():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = pathlib.Path(__file__).with_name("node_virtuals_fund_policy_test.mjs")
    result = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "signed identity/risk and exact AGPD-1 paths ok" in result.stdout
