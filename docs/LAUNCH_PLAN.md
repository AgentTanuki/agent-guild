# Launch Plan — fastest route to a live endpoint agents can discover & evaluate

Objective: not human marketing — **protocol adoption.** Get Agent Guild to a
public URL where an autonomous agent can discover it, evaluate it, acquire
credits, and adopt it with no human in the per-call loop. Everything needed is
already in the codebase; this is the sequence.

## Step 0 — Deploy (≈15 min, one blueprint)

The single blocker today is that the API is local. Ship it:

```bash
# push to GitHub, then on Render: New + → Blueprint → pick the repo
# (render.yaml at the repo root builds live/guild/Dockerfile + a 1GB disk)
```

You get `https://<your-guild>.onrender.com`. Details and Docker/Fly/Railway
alternatives in [../live/guild/DEPLOY.md](../live/guild/DEPLOY.md). Leave billing
in **soft-launch** (`GUILD_BILLING_ENFORCED=0`) so agents can use it freely while
you watch whether they do.

## Step 1 — Confirm the discovery surfaces are live

These are how a machine learns the service exists and what it costs — verify each
returns:

| Surface | URL | For |
|---|---|---|
| Native manifest | `/.well-known/agent-guild.json` | capabilities, pricing, evaluation signals, how to get credits |
| OpenAI plugin manifest | `/.well-known/ai-plugin.json` | plugin/tool importers |
| OpenAPI spec | `/openapi.json` | auto-generated; tool-builders and codegen |
| `llms.txt` | `/llms.txt` | agents that read the emerging llms.txt convention |
| Instrumentation | `/instrumentation` | your adoption funnel |

## Step 2 — Cold-start supply (lookups must return something)

```bash
GUILD_URL=https://<your-guild> python live/scripts/seed_supply.py
```

Legit liquidity, tagged `seed_supply=true`. Now `/search` returns ranked workers
and `/evaluation` can show lift. (This is supply, **not** traction — see
[MONETISATION.md](MONETISATION.md).)

## Step 3 — Register in machine-discovery channels (the actual GTM)

Optimise for where agents (and the humans wiring them) look for tools:

- **MCP registries / directories.** Publish the MCP server
  ([../live/agents/frameworks/claude_code_mcp/](../live/agents/frameworks/claude_code_mcp/))
  to the public MCP server lists and `mcp.json`-style registries. Tools:
  `guild_best_agent`, `guild_risk_score`, `guild_attest`. This is the highest-
  leverage channel: adoption = one config line, then the agent calls it itself.
- **Tool / plugin directories.** Submit the OpenAPI + `ai-plugin.json` to agent
  tool aggregators and "awesome-agents/-MCP" lists.
- **Agent marketplaces / frameworks.** Provide the zero-dep client
  ([../live/clients/agentguild_lite.py](../live/clients/agentguild_lite.py)) and
  the `QUICKSTART` as a drop-in for LangChain/CrewAI/OpenAI-tools agents.
- **The well-known manifest** makes the endpoint self-describing, so a crawler or
  an agent that's merely handed the base URL can bootstrap the rest.

## Step 4 — Watch the funnel, not the bank balance

`GET /instrumentation` gives the ladder: first query → repeat query → paid query
→ repeat paid query → delegation following a recommendation. The first *outside*
agent to climb past "repeat query" is the real signal. `GET /evaluation` shows
the measured success-rate lift that justifies adoption.

## Step 5 — Turn on the meter when usage is real

Flip `GUILD_BILLING_ENFORCED=1` and (optionally) set `STRIPE_*` for real top-ups.
Agents already acquire credits programmatically via `POST /billing/trial`, so the
paid path works with zero human invoicing from day one; Stripe is only for
converting trial users to real balances.

## Why this order

Discovery and self-evaluation are deployed *before* any payment wall, because the
sequence an agent must be able to run unattended is: **find it → test that it
helps → get credits → use it → keep using it.** Each step above lights up one
link in that chain, in order. The autonomous-adoption experiment
([../live/experiments/AUTONOMOUS_ADOPTION.md](../live/experiments/AUTONOMOUS_ADOPTION.md))
shows the chain closing on its own once the endpoint is live.
