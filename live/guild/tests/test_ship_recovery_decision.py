"""Red-gate recovery decision — deterministic, no network, no workflow run.

A healthy release (061dcea, 2026-07-31) was auto-reverted because the gate
stopped waiting before Render finished deploying. The gate was right to fail —
the release was uncertified — but the RECOVERY was wrong: production was still
serving the previous release, so reverting main changed nothing live and
pushed a second build through the pipeline that had just timed out.

These lock the corrected decision. They are pure-function tests on purpose:
the bug lived in a branch of a workflow that only executes on a red release,
which is precisely the path nobody exercises until it matters.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import ship_decision  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts",
                       "ship_decision.py")
_GATE = os.path.join(os.path.dirname(__file__), "..", "..", "scripts",
                     "release_gate.py")
_SHIP = (pathlib.Path(__file__).resolve().parents[3]
         / ".github" / "workflows" / "ship.yml")


def test_deployment_not_arrived_halts_instead_of_reverting():
    out = ship_decision.recover(failed_branch="ship/topic",
                                gate_outcome="deployment_not_arrived")
    assert out["action"] == "halt_deploy_not_arrived"
    assert "still serving the PREVIOUS release" in out["reason"]


def test_a_live_defective_release_is_still_reverted():
    """The gate is NOT weakened: code that IS live and bad still gets rolled
    back through the same certified loop."""
    for outcome in ("", "defective", "signed_decision_failed", "unknown"):
        assert ship_decision.recover(
            failed_branch="ship/topic", gate_outcome=outcome)["action"] == "revert"


def test_a_revert_branch_never_reverts_again():
    for outcome in ("", "deployment_not_arrived", "defective"):
        assert ship_decision.recover(
            failed_branch="ship/revert-abc123",
            gate_outcome=outcome)["action"] == "halt_revert_loop"


def test_the_cli_passes_the_gate_outcome_through():
    out = subprocess.run(
        [sys.executable, _SCRIPT, "recover", "--failed-branch", "ship/x",
         "--gate-outcome", "deployment_not_arrived"],
        capture_output=True, text=True, check=True).stdout
    assert "halt_deploy_not_arrived" in out


def test_the_cli_defaults_to_revert_without_an_outcome():
    """A missing attestation must not silently disable rollback."""
    out = subprocess.run(
        [sys.executable, _SCRIPT, "recover", "--failed-branch", "ship/x"],
        capture_output=True, text=True, check=True).stdout
    assert '"revert"' in out


def test_the_gate_records_a_machine_readable_outcome():
    """ship.yml reads `outcome` from the attestation; if the gate stopped
    writing it, recovery would silently fall back to always-revert."""
    src = open(_GATE).read()
    assert 'attestation["outcome"] = verdict' in src
    assert "deployment_not_arrived" in src


def test_the_deploy_wait_is_raised_but_still_bounded():
    src = open(_GATE).read()
    assert "default=1800.0" in src, "the deploy wait must be the raised bound"
    assert "raise TimeoutError" in src, "an unbounded wait is not a gate"


def test_late_deploy_has_a_supported_recertification_path():
    src = _SHIP.read_text()
    assert "workflow_dispatch:" in src
    assert "recover_deployment:" in src
    assert "certify production against the exact current main SHA" in src
    assert "registry_publish_needed.py" in src


def test_only_a_failed_release_gate_can_trigger_a_revert():
    """A registry outage after a green deploy must never roll production back."""
    src = _SHIP.read_text()
    assert ("if: failure() && steps.release_gate.outcome == 'failure'"
            in src)
    assert "if: failure() && steps.merge.outputs.merged == 'true'" not in src


def test_certified_recovery_waits_for_branch_policy_without_bypass():
    """Green CI may precede GitHub's mergeability update by a few seconds."""
    src = _SHIP.read_text()
    assert 'gh pr merge "$rnumber" --squash --delete-branch --auto' in src
    assert 'while [ -z "$recovery_sha" ] && [ $tries -lt 120 ]' in src
    assert 'certified recovery PR did not merge within 10 minutes' in src
    assert 'gh pr merge "$rnumber" --admin' not in src
