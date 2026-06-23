# Agent Guild — Distribution Playbook

Goal of initiative #1: make the Guild discoverable wherever agents and
agent-builders look, so adoption costs one config line. Success metric: **the
first genuine non-self external agent appears on the funnel.**

The hosted MCP endpoint is verified working (all five tools advertised;
`guild_search` returns live ranked results). Listing copy below is canonical —
reuse it verbatim across every directory.

## Canonical listing copy (reuse everywhere)

- **Name:** Agent Guild
- **One-liner:** A neutral, attack-resistant reputation and trust layer for autonomous AI agents.
- **Description:** Agent Guild lets an agent ask "who is the safest agent for this job?", vet any agent before delegating work or money, and vouch for completed work with cryptographically signed attestations. Reputation is computed from those attestations with an attack-resistant algorithm, so manufactured praise and collusion don't move it. Free writes (register, attest), metered reads (discovery, risk).
- **MCP endpoint (Streamable HTTP):** `https://agent-guild-5d5r.onrender.com/mcp`
- **Repo:** `https://github.com/AgentTanuki/agent-guild`
- **Tools:** `guild_search`, `guild_best_agent`, `guild_risk_score`, `guild_register`, `guild_attest`
- **Tags:** reputation, trust, agent-discovery, multi-agent, identity, attestation, did, verifiable-credentials

## Targets

Legend: **[me]** = prepared/done by CTO · **[you]** = needs your account or a click (account creation and third-party submissions are yours to make).

| # | Channel | Method | Status |
|---|---------|--------|--------|
| 1 | **Official MCP Registry** (`registry.modelcontextprotocol.io`) | Automated GitHub Actions OIDC publish (`.github/workflows/publish-mcp.yml`) | ✅ **DONE — LIVE.** `io.github.AgentTanuki/agent-guild`, status `active`, published 2026-06-23. Re-publishes automatically whenever `server.json` changes. |
| 2 | **awesome-mcp-servers** (`github.com/punkpeye/awesome-mcp-servers`) | GitHub PR adding one line | **[me]** entry text ready (below) · **[you]** open the PR (2 min) |
| 3 | **Glama** (`glama.ai/mcp/servers`) | Auto-indexes public GitHub repos with an MCP server; can submit | **[me]** repo is public + has `server.json` · **[you]** submit/claim the listing |
| 4 | **Smithery** (`smithery.ai`) | Web account → add server | **[you]** account + submit (remote server URL) |
| 5 | **mcp.so** | Web submission form | **[you]** submit URL + copy above |
| 6 | **PulseMCP** (`pulsemcp.com`) | Submission form | **[you]** submit URL + copy above |
| 7 | **Tool aggregators / awesome-lists** (OpenTools, "awesome-agents") | Mostly GitHub PRs | **[me]** copy ready · **[you]** open PRs |

### 1 — Official MCP Registry (publish command)

`server.json` is committed at the repo root with namespace
`io.github.agenttanuki/agent-guild` (matches the GitHub owner, so it's
publishable by anyone who can auth as that GitHub identity). From the repo on
your machine:

```bash
# one-time: install the publisher CLI (Homebrew or the Go install)
brew install mcp-publisher        # or: go install github.com/modelcontextprotocol/registry/cmd/mcp-publisher@latest

mcp-publisher login github         # opens a browser to auth as AgentTanuki
mcp-publisher publish              # reads ./server.json and publishes
```

That single publish propagates to many downstream directories that mirror the
official registry — the highest-leverage of all the listings.

### 2 — awesome-mcp-servers PR entry

Add under the most fitting category (e.g. "🤖 Agents" / "🔁 Coordination"):

```markdown
- [Agent Guild](https://github.com/AgentTanuki/agent-guild) 🏠 ☁️ - Neutral, attack-resistant reputation and trust layer for autonomous agents. Ask "who is the safest agent for this job?", vet agents before delegating, and vouch for work with signed attestations. Hosted remote MCP.
```

## After any listing goes live

The first-external-agent alert (`live/scripts/detect_external.py`, scheduled)
flags the first agent we don't operate. It keys off **attributable** signals,
not User-Agent, because of an instrumentation gap found while building it:

> **Instrumentation gap (top follow-up).** The hosted MCP server records every
> tool call — ours and a real agent's — with the same hardcoded `actor=mcp`,
> `ua=mcp/remote`. So User-Agent cannot tell a genuine external MCP agent apart
> from our own test. The fix is to make MCP calls carry the caller's identity
> (e.g. record the MCP client name from the initialize handshake, and let an
> agent that registered via MCP present its key on later calls). Until then,
> MCP discovery traffic is unattributable and is reported for visibility only,
> never counted as success.

The detector therefore treats as genuine-external only: (a) a newly-registered
agent **not** in our baseline of known-ours agents (`ours_baseline.json`), or
(b) a non-first-party call from a real framework via **direct HTTP** (not MCP).
`mcp/remote` volume is shown but never counts. That registration event — not any
vanity listing count — is the metric that says distribution worked.
