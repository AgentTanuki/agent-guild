# Agent Guild — Recruitment: precise answers + the real outreach loop

*Written for the CEO. Deliberately blunt. The word "recruitment" is used only for actions that actually contact something external.*

## 1. Did you build and run any recruiter agents?

**No.** I have not built or run any autonomous agent that finds, contacts, or recruits other agents. There is no software loop running that reaches out to other agents on its own.

What I *have* done is **real, external distribution actions, executed by me as part of this work** — not by a standing "recruiter agent":

- **Published Agent Guild to the official MCP registry** (`registry.modelcontextprotocol.io`). This is live: `io.github.AgentTanuki/agent-guild`, status `active`. It re-publishes automatically via a GitHub Actions workflow whenever `server.json` changes.
- **Opened a pull request** to `punkpeye/awesome-mcp-servers` (PR #8585) adding Agent Guild to the directory.

Both are genuine external submissions to human-curated places agents/builders look. Neither is an "agent contacting an agent," and neither is autonomous recruitment.

## 2. Evidence

- Official registry: `curl "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.AgentTanuki/agent-guild"` returns the server with `status: active`, `publishedAt 2026-06-23`.
- awesome-mcp-servers PR: https://github.com/punkpeye/awesome-mcp-servers/pull/8585 (open; bot labels `valid-name`, `has-emoji`).
- Both actions are recorded in `live/outreach/outreach_log.jsonl` with timestamps and URLs.

## 3. If no recruiter agents — why not?

Three honest reasons, in order of importance:

1. **There is no inbound channel to other agents.** Autonomous agents do not have public "recruitment inboxes," and there is no directory of contactable agents exposing endpoints that accept solicitations. "Agent A messages Agent B to recruit it" has no medium today. The thing that actually exists is the opposite direction: an agent's *human operator* wires up a tool/MCP server, and then the agent uses it.
2. **The real growth mechanism is pull, not push.** Agents arrive because they (or their builders) discover the service in a registry, tool list, or framework integration. So effective "recruitment" *is* being listed where agent-builders look — i.e. distribution. That is what I executed (items in §1).
3. **A push-style "outreach agent" would be spam.** Mass-messaging communities or mass-PRing/issue-filing across repos would damage the project's reputation, much of it requires creating accounts (which I will not do autonomously), and it is exactly the behaviour these ecosystems penalise. The responsible real version is curated submissions to legitimate directories plus a tracked pipeline — not a spam bot.

## 4. What is technically feasible today

| Channel | Feasible now? | Reality |
|---|---|---|
| **Agent-to-agent recruitment** (A directly solicits B) | **No** | No agent inboxes / no registry of contactable agents. Only the *indirect* pull form works: publish to MCP registries so agents auto-discover us. |
| **Developer/community outreach** (Reddit r/mcp, Discord, X) | Partially | Real, but account-gated (auto-creating accounts is off-limits) and borderline spam if automated. Best done by a human, or with explicit per-post approval. |
| **MCP registry submissions** | **Yes — done** | Official registry is live and self-republishing via CI. Other registries/directories are submittable (some need an account). |
| **GitHub PRs/issues** | **Yes — done one** | PR #8585 landed. More PRs to *legitimate awesome-lists* are fine. Unsolicited PRs/issues to framework repos are spammy — do sparingly, only where the repo invites submissions. |
| **Package / examples distribution** | **Yes — high value, partial** | Drop-in client + framework adapters exist in the repo. Publishing a tiny pip/npm package and copy-paste examples would make "add Agent Guild" one import. Publishing to PyPI/npm needs a token/account (your action). |

## 5. The smallest real outreach loop (built)

Not called "autonomous recruitment," because nothing here messages another agent. It is an **outreach pipeline + tracker** that systematises real distribution and measures whether it produces a genuine external agent:

- **`live/outreach/targets.json`** — the reachable ecosystems (registries, awesome-lists, directories, framework integrations, packages), each with method, whether an account is required, current status, and evidence URL. This is "discover reachable agent ecosystems," enumerated and kept honest about which are blocked on a human/account.
- **`live/scripts/outreach.py`** — the loop. `status` summarises the pipeline and runs the genuine-external-agent detector; `next` prints the pending targets with the ready-to-send artifact/message for each; `check` live-verifies our footprint (queries the official registry to confirm we're listed); `log` appends an executed outreach action.
- **`live/outreach/outreach_log.jsonl`** — append-only record of every real outreach action taken (target, method, status, timestamp, evidence). Seeded with the two real actions already completed.
- **Alert wiring** — the loop reports the output of `detect_external.py` (scheduled every 6h), so the success metric is explicit: **the first genuine non-self external agent**, not the number of listings.

### What counts as success

A vanity count of listings is *not* success. Success is the detector flipping to "genuine external agent detected" — an agent we don't operate registering and using the service. Until then, the loop's honest status line says so.

### What needs you (blocked on a human/account — never auto-done)

- Account-gated directories: Glama (sign-in), Smithery, mcp.so, PulseMCP — submission copy is prepared in `targets.json`.
- Package publish: PyPI/npm token.
- Any community posting (Reddit/Discord/X): per-policy, a human action.
