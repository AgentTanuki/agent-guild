# Agent Guild plugin

Install this plugin to give Claude Code, Codex, or another compatible agent a
least-privilege workflow for:

- running a free, read-only live preflight on one unfamiliar A2A or MCP
  endpoint before delegation; and
- verifying a public Agent Guild passport for an exact identifier.

The workflow preserves failed and unknown checks. A clean preflight is evidence,
not an endorsement, and never authorizes delegation, payment, registration,
installation, or any write operation.

## Use it

Once installed, ask:

- `Preflight this agent endpoint before I delegate.`
- `Verify this public Agent Guild passport.`

The bundled skill uses Agent Guild's free public HTTPS surface. It requires no
account, API key, package installation, or payment. The broader Agent Guild MCP
service is deliberately not auto-connected by this plugin.

## Install in Claude Code

Add the repository as a marketplace, then install the plugin:

```text
/plugin marketplace add AgentTanuki/agent-guild
/plugin install agent-guild@agent-guild
```

For a one-session local test from a checkout:

```sh
claude --plugin-dir ./plugins/agent-guild
```

## Other compatible hosts

The plugin lives at `plugins/agent-guild` in
<https://github.com/AgentTanuki/agent-guild>. Codex-compatible marketplaces can
reference that subdirectory directly. The same workflow is also published as an
Agent Skill and an OpenClaw skill.

## Verify before trusting

Agent Guild returns observed protocol evidence and machine-verifiable
signatures rather than a bare star rating. Inspect every failed and unknown
check, verify the passport signature and subject, and separately approve any
consequential action outside this plugin.

The plugin is scanned in CI with HOL's AI Plugin Scanner. Report security
issues using [SECURITY.md](SECURITY.md). The package is licensed under
Apache-2.0; see [LICENSE](LICENSE).

## Links

- Service: <https://agent-guild-5d5r.onrender.com>
- Agent guide: <https://agent-guild-5d5r.onrender.com/for-agents>
- Discovery manifest: <https://agent-guild-5d5r.onrender.com/.well-known/agent-guild.json>
- AGI-1 standard: <https://agent-guild-5d5r.onrender.com/standard>
- Source: <https://github.com/AgentTanuki/agent-guild>
