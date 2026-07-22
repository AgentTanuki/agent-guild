#!/usr/bin/env python3
"""ship_decision — the merge/recovery decision core of the machine ship loop.

.github/workflows/ship.yml delegates every judgement call to this script so
the logic is UNIT-TESTED (tests/test_ship_decision.py) instead of living as
untestable bash inside YAML. The workflow gathers the facts (git ancestry,
branch protection, PR head, the SHA ci certified) and this decides.

Merge decision (`decide`), strict precedence:

  1. refuse_unprotected      — main is not branch-protected. The loop REFUSES
                               to operate without protection: required PRs +
                               required non-release ci checks + strict
                               up-to-date + no bypass are mandatory, not
                               etiquette (corrective round 2, defect 2).
  2. refuse_head_mismatch    — the PR head is not the SHA ci certified. A
                               newer push carries its own certification;
                               merging anything else would ship untested code.
  3. update_and_recertify    — ci certified the branch head, but main has
                               advanced since: the COMBINED state was never
                               tested (defect 1). The workflow updates the
                               branch with main, dispatches ci again, and
                               refuses to merge this round.
  4. merge                   — head == certified AND main is an ancestor of
                               that head: the certified tree IS the tree main
                               will hold after the squash merge.

Recovery decision (`recover`) after a red release gate (defect 4):

  * revert                   — open ship/revert-<sha> through the SAME
                               certified loop: revert commit → PR → ci →
                               auto-merge → gate certifies the recovery.
                               An issue is filed as telemetry, never as the
                               recovery mechanism.
  * halt_revert_loop         — the FAILED branch was itself a revert: a
                               revert-of-a-revert would oscillate forever.
                               Stop, keep the issue, demand nothing of any
                               human except reading it.
"""
from __future__ import annotations

import argparse
import json
import sys

MERGE_ACTIONS = ("refuse_unprotected", "refuse_head_mismatch",
                 "update_and_recertify", "merge")
RECOVERY_ACTIONS = ("revert", "halt_revert_loop")

REVERT_PREFIX = "ship/revert-"


def decide(*, protected: bool, pr_head: str, certified: str,
           main_is_ancestor_of_head: bool) -> dict:
    """The merge decision for one certified-ci completion. Pure function."""
    if not protected:
        return {"action": "refuse_unprotected",
                "reason": "main has no branch protection; the loop refuses "
                          "to merge until required PRs + required ci checks "
                          "+ strict up-to-date + no-bypass are enforced"}
    if pr_head != certified:
        return {"action": "refuse_head_mismatch",
                "reason": f"PR head {pr_head[:12]} != certified "
                          f"{certified[:12]}; the newer push will arrive "
                          "with its own certification"}
    if not main_is_ancestor_of_head:
        return {"action": "update_and_recertify",
                "reason": "main advanced after certification; the combined "
                          "state was never tested — update the branch with "
                          "main, dispatch ci, merge only the re-certified "
                          "result"}
    return {"action": "merge",
            "reason": "head is exactly the certified SHA and already "
                      "contains current main — the certified tree is the "
                      "post-merge tree"}


def recover(*, failed_branch: str) -> dict:
    """The recovery decision after a red release gate for a merged ship."""
    if failed_branch.startswith(REVERT_PREFIX):
        return {"action": "halt_revert_loop",
                "reason": "the failed ship was itself an automatic revert; "
                          "a revert-of-a-revert would oscillate — halting "
                          "with the issue as the only remaining signal"}
    return {"action": "revert",
            "reason": "open ship/revert-<sha> through the same certified "
                      "loop so the ROLLBACK is tested, merged, deployed and "
                      "gate-certified exactly like any other ship"}


def _bool(s: str) -> bool:
    if s.lower() in ("true", "1", "yes"):
        return True
    if s.lower() in ("false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got {s!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("merge", help="merge decision")
    m.add_argument("--protected", type=_bool, required=True)
    m.add_argument("--pr-head", required=True)
    m.add_argument("--certified", required=True)
    m.add_argument("--main-is-ancestor", type=_bool, required=True)
    r = sub.add_parser("recover", help="red-gate recovery decision")
    r.add_argument("--failed-branch", required=True)
    args = ap.parse_args()
    if args.cmd == "merge":
        out = decide(protected=args.protected, pr_head=args.pr_head,
                     certified=args.certified,
                     main_is_ancestor_of_head=args.main_is_ancestor)
    else:
        out = recover(failed_branch=args.failed_branch)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
