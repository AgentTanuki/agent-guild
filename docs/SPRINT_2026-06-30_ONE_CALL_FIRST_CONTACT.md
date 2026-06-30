# Sprint Review — One-Call First Contact: convert the live discovery channel (2026-06-30)

**Frame:** remove the *current limiting factor* in network growth, not "ship a
feature." North Star: maximise the probability an AI agent discovers, trusts,
connects to, contributes to, and recommends Agent Guild.

## Step 1 — Observe
Since the prior sprint, `/evaluation` is honest and non-null (provenance-labelled
bootstrap). The funnel is structurally complete and the trust/proof blockers are
gone. The one metric still flat: **zero genuine third-party calls.**

## Step 2 — The limiting factor
Not another trust/quality fix. The binding constraint is **adoption**: a
discovering agent isn't converting into a first real call. Raw demand generation
(outreach, PRs, social) is human-gated and needs Ross's accounts. But one demand
channel is **already live and ungated** — passive directory discovery (MCP
Registry, Smithery, Glama, mcp.so). The active constraint on *that* channel is
**conversion**: a connected/aware agent must decide to call, and first contact
required chaining four tools (search → risk → register → attest) before the value
and the proof were visible. High activation energy on the only live channel.

## Step 3 — Highest-leverage intervention
Collapse first contact into a **single self-proving call**. Decision rationale
against the hierarchy:
- **Strengthens the network?** Yes — more first calls → more attestations → richer
  graph.
- **Creates/strengthens a flywheel?** Yes — it's the entry to the
  contribution→quality→recommendation loop.
- **Compounds?** Yes — every improvement to first-call conversion multiplies the
  return on *all* discovery (the already-live channel and any future outreach).
- **Buildable now, no gating?** Yes — entirely in-repo.

Alternatives considered and deferred: a machine-readable `/metrics` reliability
artifact (marginal — removes a reason to decline, doesn't create a call); a
portable reputation passport (interoperability/defensibility, but speculative with
zero current users); referral-loop polish (multiplies growth, but only once users
exist). The one-call entry dominates because it acts on the *active* constraint of
the *live* channel.

## Step 4 — Implement (all in-repo, pushable)

| # | Change | Files |
|---|--------|-------|
| 1 | **`Store.check(capability)`** — one tested composer returning `best_agent`, hire/avoid `verdict`, `shortlist`, provenance-labelled `proof` (from `/evaluation`), `why_trust_this`, `how_to_contribute`. Plus shared `Store.shortlist()` / `Store.risk_for()` so MCP, `/search`, `/risk-score` and `/check` use one ranking + one risk formula. | `live/guild/app/store.py` |
| 2 | **`guild_check` MCP tool** ("START HERE") + **`GET /check` HTTP endpoint** — the same payload on both transports; each records exactly one genuine-external activation event. | `live/guild/app/mcp_server.py`, `main.py` |
| 3 | **All discovery surfaces lead with it** — MCP server `instructions`, manifest (`start_here`, `endpoints.check`, `discovery.mcp.start_here_tool` + tool list), `llms.txt` ("Start here (one call)"), README (six-tool intro, `/check` quickstart, tool-table row). | `mcp_server.py`, `main.py`, `README.md` |
| 4 | **Tests** — payload shape, proof carries the dataset label, graceful unknown-capability, `/check` agrees with the granular paths, HTTP endpoint + manifest/llms pointers. | `tests/test_check_onboarding.py`, `tests/test_agent_native.py` |

**Verified end-to-end** (bootstrap-seeded server, one `GET /check?capability=fact-check`):
best `Veritas-Prime` (trust 44.2) · verdict **hire** (risk 22.2) · shortlist of 3 ·
proof `dataset: bootstrap, lift +0.506` (87.5% vs 36.9%) · contribution hint
present. Full suite **59 passed** (54 prior + 5 new). MCP server registers
`guild_check`; imports clean.

## Step 5 — Re-evaluate: the next limiting factor
With conversion of the live channel maximised, the binding constraint reverts to
**volume on that channel** — i.e. genuine distribution/awareness, which needs
Ross's authority (posting as AgentTanuki, the awesome-mcp-servers PR #8585 nudge,
PulseMCP/mcp.so review, agent-guild.ai domain). That is the gated bottleneck. The
highest-leverage *buildable* follow-ups while that's pending:
1. **Machine-readable `/metrics`** (uptime, p50/p95, error rate) — automated tool
   selectors and directories rank on measurable reliability.
2. **Activation-only referral loop** so each genuine first caller recruits the
   next — turns the flywheel without human marketing.
3. **Portable reputation passport** (exportable signed VC) — makes the Guild the
   reputation *source of record* even off-platform (interoperability + defensibility).

## Step 6 — Repeat
Not finished. The next constraint (distribution volume) is now explicit and
largely gated; the buildable backlog above is ranked and ready.
