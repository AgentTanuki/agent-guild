#!/usr/bin/env bash
# One-time, admin-credentialed: enforce the branch protection the machine
# ship loop REQUIRES (.github/workflows/ship.yml refuses to merge without it).
#
#   * pull requests required before merging (0 approvals — approvals would
#     put a human in the loop; certification comes from ci, not from eyes)
#   * every non-release ci job is a required status check
#   * strict up-to-date: the branch must contain current main at merge time
#   * enforce_admins: nobody bypasses, including the repo owner
#   * no force pushes, no deletions
#
# Run once with an admin-authenticated gh:  bash live/scripts/protect_main.sh
# (GITHUB_TOKEN inside Actions cannot administer branch protection — this is
# deliberately the ONE settings action that stays with the repo owner.)
set -euo pipefail

REPO="${1:-AgentTanuki/agent-guild}"

gh api -X PUT "repos/$REPO/branches/main/protection" \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "test (json)",
      "test (sqlite)",
      "strict-kdf",
      "contract",
      "independent-vc-verification",
      "caller-proof-wallet-verification",
      "trustplane",
      "trustplane-integrations (crewai)",
      "trustplane-integrations (langchain)",
      "trustplane-integrations (openai-agents)",
      "trustplane-integrations (mcp)",
      "x402-interop"
    ]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 0
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": false,
  "lock_branch": false,
  "allow_fork_syncing": false
}
JSON

echo
echo "main is now protected. Verify: gh api repos/$REPO/branches/main --jq .protected"
echo "The ship loop's refuse_unprotected halt lifts on the next certified run."
