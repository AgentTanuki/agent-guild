"""Machine ship loop decision core (live/scripts/ship_decision.py).

The release loop's judgement calls — merge / refuse / update-and-recertify /
revert — are pure functions here so they carry REAL regression coverage
instead of living as bash in workflow YAML. Locks (corrective round 2):

  * a stale ship branch (main advanced after certification) is NEVER merged:
    it must be updated with main and re-certified first;
  * two concurrently certified branches can never both land against the same
    main — the second becomes stale the instant the first merges;
  * an unprotected main refuses every merge (protection is mandatory);
  * a superseded head (newer push) is refused, not merged;
  * red-gate recovery reverts through the same certified loop, and a failed
    revert halts rather than oscillating.
"""
import importlib.util
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "live" / "scripts" / "ship_decision.py"

spec = importlib.util.spec_from_file_location("ship_decision", SCRIPT)
ship = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ship)


def _decide(protected=True, pr_head="H1", certified="H1", ancestor=True):
    return ship.decide(protected=protected, pr_head=pr_head,
                       certified=certified,
                       main_is_ancestor_of_head=ancestor)["action"]


# --- the happy path ----------------------------------------------------------

def test_certified_up_to_date_head_merges():
    assert _decide() == "merge"


# --- defect 1: stale ship branch ---------------------------------------------

def test_stale_ship_branch_is_never_merged():
    """ci certified the branch head, but main advanced afterwards: the
    combined state was never tested — the ONLY allowed action is update +
    re-certify, never merge."""
    assert _decide(ancestor=False) == "update_and_recertify"


def test_stale_branch_merges_only_after_recertification():
    """The full stale sequence: refuse now; after update-branch the new head
    (containing main) gets its own ci run, and only THAT certification
    merges."""
    # round 1: certified old head, main advanced → update + recertify
    assert _decide(pr_head="H1", certified="H1",
                   ancestor=False) == "update_and_recertify"
    # the update rewrote the head; the OLD certification no longer matches
    assert _decide(pr_head="H2", certified="H1",
                   ancestor=True) == "refuse_head_mismatch"
    # round 2: ci certified the combined head → merge
    assert _decide(pr_head="H2", certified="H2", ancestor=True) == "merge"


# --- defect 1, concurrent form: two certified branches ------------------------

def test_two_concurrently_certified_branches_cannot_both_land_unchecked():
    """Branches A and B are both certified against the same main M. A merges
    first; B's certification is now against a main that no longer exists —
    B MUST go around the loop again on the combined state."""
    # both certified against M: for A, main (M) is an ancestor of its head
    assert _decide(pr_head="A1", certified="A1", ancestor=True) == "merge"
    # A merged → main is now M_A; B's head no longer contains main
    assert _decide(pr_head="B1", certified="B1",
                   ancestor=False) == "update_and_recertify"
    # B updated with M_A → new head B2, re-certified → merge
    assert _decide(pr_head="B2", certified="B1",
                   ancestor=True) == "refuse_head_mismatch"
    assert _decide(pr_head="B2", certified="B2", ancestor=True) == "merge"


# --- defect 2: protection is mandatory ----------------------------------------

def test_unprotected_main_refuses_every_merge():
    assert _decide(protected=False) == "refuse_unprotected"
    # ...and protection outranks every other consideration
    assert _decide(protected=False, ancestor=False) == "refuse_unprotected"
    assert _decide(protected=False, pr_head="H2",
                   certified="H1") == "refuse_unprotected"


# --- superseded pushes ---------------------------------------------------------

def test_superseded_head_is_refused_not_merged():
    """A newer push arrived after certification: the certified SHA is no
    longer the PR head. Refuse — the newer push carries its own ci run."""
    assert _decide(pr_head="H2", certified="H1") == "refuse_head_mismatch"


# --- defect 4: machine-complete recovery ---------------------------------------

def test_red_gate_recovery_reverts_through_the_same_loop():
    out = ship.recover(failed_branch="ship/corrective-pass-0722")
    assert out["action"] == "revert"
    assert "certified" in out["reason"]


def test_failed_revert_halts_instead_of_oscillating():
    out = ship.recover(failed_branch="ship/revert-578c91c")
    assert out["action"] == "halt_revert_loop"


def test_action_vocabulary_is_closed():
    """The workflow switches on these exact strings — lock the vocabulary."""
    assert ship.MERGE_ACTIONS == ("refuse_unprotected", "refuse_head_mismatch",
                                  "update_and_recertify", "merge")
    assert ship.RECOVERY_ACTIONS == ("revert", "halt_revert_loop")
