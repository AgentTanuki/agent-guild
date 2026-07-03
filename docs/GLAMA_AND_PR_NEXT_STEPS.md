# Next steps: clear the awesome-mcp-servers PR + claim Glama

Two short jobs. The first nudges the open directory PR; the second gives you
ownership/analytics of the Glama listing. Do them in this order — claiming
Glama first means the PR comment can point at a verified listing.

---

## Part A — Claim the Glama connector (verify ownership)

Glama verifies ownership by reading a file served at your MCP server's own
domain: `https://agent-guild-5d5r.onrender.com/.well-known/glama.json`. The
email in that file must match the email on your Glama account.

**I've already added the route to the server** (`live/guild/app/main.py`), so
once you deploy, the file goes live automatically. It returns:

```json
{
  "$schema": "https://glama.ai/mcp/schemas/connector.json",
  "maintainers": [{ "email": "<glama-account-email>" }]
}
```

### Steps

1. **Confirm the email.** The route uses `<glama-account-email>`. If the email on
   your Glama account is different, change it in `live/guild/app/main.py`
   (search for `wellknown_glama`) before deploying — they must match exactly.

2. **Commit and deploy** the change (this session has no GitHub credentials, so
   you run these):
   ```bash
   cd "/Users/rossburley/Claude/Projects/Agent Guild"
   git add live/guild/app/main.py
   git commit -m "Add /.well-known/glama.json for Glama ownership verification"
   git push
   ```
   Render auto-deploys from the repo. Wait ~1–2 min for the deploy to finish.

3. **Verify the file is live.** Open in a browser (or curl):
   `https://agent-guild-5d5r.onrender.com/.well-known/glama.json`
   You should see the JSON above.

4. **Sign in to Glama** at https://glama.ai with the GitHub account whose email
   matches (`<glama-account-email>`).

5. **Open the connector page** and click **Claim / verify ownership**:
   https://glama.ai/mcp/connectors/io.github.AgentTanuki/agent-guild
   Glama auto-detects the `.well-known/glama.json` file within a few minutes and
   marks the listing as claimed.

6. Once claimed you get: control over the description/metadata, usage analytics,
   and health/monitoring reports.

> If auto-detection doesn't trigger, email support@glama.ai with the connector
> URL — they can verify manually.

---

## Part B — Nudge the open awesome-mcp-servers PR (#8585)

Status as of 2026-06-29:
- **PR #8585** (Other Tools & Integrations) — **OPEN**, still labelled
  `missing-glama`. This is the one to push.
- **PR #8622** (Aggregators) — closed unmerged on 2026-06-25. The duplicate
  resolved itself; ignore it.

The `missing-glama` label is set by the repo's bot and just hasn't re-scanned
since the Glama listing appeared. A comment linking the live Glama page usually
prompts a re-check.

### Steps

1. Go to https://github.com/punkpeye/awesome-mcp-servers/pull/8585
2. Sign in as **AgentTanuki**.
3. Paste the comment below into the comment box and post it.
4. (Optional) Once Part A is done and the listing is claimed, you can edit the
   comment to say "claimed/verified on Glama" for extra weight.

### Comment to paste

```
This is now listed on Glama: https://glama.ai/mcp/connectors/io.github.AgentTanuki/agent-guild

The connector is live and health-checked (status: Healthy), with all 5 tools
indexed and a Tool Definition Quality score of A (3.6/5). Could the `missing-glama`
label be re-evaluated when you get a chance? Happy to adjust the entry if anything
else is needed. Thanks for maintaining the list!
```

---

## Quick reference

| Item | Link |
|------|------|
| Glama connector | https://glama.ai/mcp/connectors/io.github.AgentTanuki/agent-guild |
| Open PR | https://github.com/punkpeye/awesome-mcp-servers/pull/8585 |
| Live well-known file (after deploy) | https://agent-guild-5d5r.onrender.com/.well-known/glama.json |
