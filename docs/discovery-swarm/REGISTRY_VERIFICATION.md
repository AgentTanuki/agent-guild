# Registry verification records

Verified live 2026-07-10 (post-fix). "Discoverable" = found through the registry's own search/API AND the listing points at a working production endpoint. A submission, a stale cache, or a URL that only works when already known does NOT count as discoverable.

## MCP official registry — DISCOVERABLE ✓

- Identifier: `io.github.AgentTanuki/agent-guild`
- Indexed: yes — returned by `GET /v0/servers?search=agent-guild`; two versions present, `1.1.0` is `isLatest:true`, both `status: active`.
- Manifest endpoint: `https://agent-guild-5d5r.onrender.com/mcp` — handshake verified 200 (was 421 before the guard fix; the listing was pointing at a dead endpoint for days).
- Description: current and accurate.
- Rules compliance: remote streamable-http server with a reachable URL — compliant.
- Last discovery test: 2026-07-10, registry-led cold-discovery LLM client found AG here and completed guild_check.
- Action: none. Optionally deprecate the `1.0.0` record so only `1.1.0` shows.

## a2aregistry.org — LISTED, HEALTH-VERIFIED, but SKILLS SNAPSHOT STALE ⚠️

- Identifier: name "Agent Guild", `url: …/a2a`, `wellKnownURI: …/.well-known/agent-card.json`.
- Indexed: yes — in `GET /api/agents` (50 agents). Health checker ran today 09:52 UTC → `task_conformance: WORKING, passed:true`, uptime 98.4%, `is_healthy:true`. So the registry actively re-checks liveness.
- **Gap: the registry stores its OWN copy of `skills` (2: `guild.check`, `guild.capabilities`) — the pre-swarm card. Production now serves 19 skills** (`guild.*` + `guild.invoke` + 16 `ag.*`) at both `/.well-known/agent.json` and `/.well-known/agent-card.json` (verified: 19 each). The registry re-checks HEALTH but did not refresh the skills snapshot. This is the measured cause of every registry-led data-task miss in Test A — a machine reading the registry's stored skills never sees the utility capabilities.
- Rules compliance: fine (live hosted A2A agent, verified card). The stale copy is the registry's, not a violation.
- Refresh path: a2aregistry is "git-as-database on GitHub" + a Python SDK; refreshing the stored snapshot requires a submission/PR to their repo (or their re-ingest of our wellKnownURI). **This is an outbound public-content submission → requires Ross's go-ahead.** Corrected payload prepared: `harness/results/a2aregistry_entry_corrected.json` (our live 19-skill card, ready to submit).
- Last discovery test: 2026-07-10 — protocol-led cold discovery found and invoked AG via the LIVE card (not the registry's stored skills), because the harness inspects the live `/.well-known/agent-card.json`. A machine trusting only the registry's stored skills would miss the utility capabilities.

## Glama — LISTED ✓

- Identifier: slug `agent-guild` (`https://glama.ai/mcp/servers/qim49mh3yh`).
- Indexed: yes — found via `?query=guild` (note: exact `?query=agent-guild` misses it, a Glama search quirk, not our record).
- Points at the MCP hosted-server page; MCP endpoint fixed and verified.
- Last discovery test: 2026-07-10 (search-level).

## Smithery — LISTED, tool snapshot STALE, gateway UNVERIFIABLE ⚠️

- Identifier: `agent-tanuki/agent-guild`.
- Indexed: yes — `GET /servers?q=guild` and the detail endpoint return the record.
- Stored tools: the pre-swarm 5 (`guild_search/best_agent/risk_score/register/attest`) — missing the 16 `ag_*` capability tools now on the live MCP surface (30 tools total).
- Endpoint: Smithery proxies via `agent-guild--agent-tanuki.run.tools` (401 without a Smithery account key) — cannot verify the invocation path end-to-end from here.
- Refresh path: re-scan/re-publish via Smithery (account action). Requires Ross.
- Last discovery test: not invocable from here (gateway auth).

## crewAI PR #6429 / maiat #176 — NOT PUBLICATIONS

Open PR/issue, framework-integration attempts, not registry listings. Do not count as discoverable.

## Summary

Discoverable & endpoint-correct today: MCP registry, Glama, a2aregistry (live card). Stale content needing an owner-gated refresh submission: a2aregistry stored skills (2→19), Smithery tools (5→30). No listing is misleading about reachability or points at a dead endpoint any longer (the 421 that broke the MCP-registry endpoint is fixed). No listing violates registry rules. Corrected a2aregistry payload staged; the actual submissions are outbound public-content actions held for Ross.
