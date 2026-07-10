# Discovery Swarm — Pilot A Delivery Report

Date: 2026-07-10 · Author: principal architect (this session) · Status: **implemented, tested, committed; deploy pending a `git push` (see §8)**

The doctrine held throughout: Agent Guild is machine-only. Nothing in this work targets humans, SEO, social, or newsletters. The one human surface — `/dashboard` — reports machine activity to you, the operator, and nothing else.

## 1. Current-state findings (summary)

Full map: `docs/discovery-swarm/current-state.md`. The service already had identity (did:key), signing/VCs, a machine-executable register→prove→attest membership funnel, a journey engine, an MCP server, an A2A card, well-known manifests, honest attribution with synthetic-traffic exclusion, a referral graph, a ledger, billing, and live registry publications. Live telemetry: **zero genuine-external registrations from cold discovery** — external traffic is registry crawlers probing `/a2a`.

The gap was not discoverability plumbing. It was that **AG had nothing an external agent could usefully invoke** before deciding to join: its only "capability" was trust lookups about other agents, capabilities were unschemaed strings, and nothing executed. Pilot A closes exactly that gap and nothing else — it extends the existing app rather than rebuilding it.

## 2. Final architecture

Full design: `docs/discovery-swarm/architecture.md`. Six cooperating layers, all in one new package `live/guild/app/swarm/`, identities as signed documents (not processes), the shared runtime being the existing FastAPI service:

- **L1 Identity Factory** (`identity.py`) — signs versioned AG identity documents from validated capabilities; publish gate tied to fixture results.
- **L2 Capability Seeds** (`capabilities.py`) — 16 narrow, deterministic, side-effect-free capabilities with schemas, fixtures, failure modes, prohibited uses.
- **L3 Discovery Mapper** (`mapper.py`) — machine-readable ecosystem map; read-only verification adapters; needs-human targets stay draft-only.
- **L4 Discovery Agents** (`agents.py`) — 5 bounded mandates through one policy chokepoint (kill switch → budget → allowlist → reason-coded log); circuit breakers; no shell/deploy/DB access.
- **L5 Acquisition Gateway** (`gateway.py`) — guest invocation, terms-before-invocation, rate limits, payload caps, kill switch, signed provenance + referral token per completion.
- **L6 Provenance & Graph** (`provenance.py`, `graph.py`) — Guild-signed envelopes; discovery/referral graph labelled organic vs internal by the existing attribution layer.

Supporting: `utility.py` (machine-native invocation-utility scoring), `experience.py` (privacy-preserving observational-learning substrate), `router.py` (all REST + dashboard).

## 3. Every file changed

New (`live/guild/app/swarm/`): `__init__.py`, `capabilities.py`, `identity.py`, `gateway.py`, `provenance.py`, `mapper.py`, `agents.py`, `graph.py`, `utility.py`, `experience.py`, `router.py`.

New tests: `tests/test_swarm_capabilities.py`, `test_swarm_identity.py`, `test_swarm_gateway.py`, `test_swarm_agents.py`, `test_swarm_e2e_external.py`.

New docs: `docs/discovery-swarm/{current-state,architecture,threat-model,implementation-plan}.md` + this report.

Modified (surgical): `app/store.py` (+`swarm_state` persisted key), `app/main.py` (include swarm router, publish-gate on startup, manifest + llms.txt wiring), `app/mcp_server.py` (17 `ag_*` tools generated from the registry), `app/a2a.py` (17 swarm skills + `invoke:` intent), `requirements.txt` (+`jsonschema`, `python-dateutil`), `server.json` (v1.2.0 description → triggers MCP Registry republish on push).

## 4. Endpoints added

`GET /.well-known/ag-identities/index.json` · `GET /identities/{ag_id}` · `GET /terms.json` · `POST /invoke/{capability_id}` · `GET /swarm/capabilities` · `GET /swarm/match?task=` · `GET /swarm/ecosystems` · `GET /swarm/stats` · `GET /swarm/graph` · `GET /swarm/agents` · `POST /swarm/agents/run` [admin] · `POST /swarm/kill` [admin] · `POST /swarm/revive` [admin] · `GET /dashboard`. Plus A2A `invoke:` intent on `POST /a2a` and 17 `ag_*` MCP tools + `ag_capabilities` on `/mcp`.

## 5. Schemas added

Each of the 16 capabilities ships a strict JSON Schema (draft 2020-12) for input and output — see `GET /swarm/capabilities`. New envelope/document schemas: `ag-identity/1`, `ag-identity-index/1`, `ag-provenance/1`, `ag-terms/1`, `ag-ecosystem-map/1`, `ag-discovery-graph/1`. New persisted store key `swarm_state` (counters, actions ledger, referral tokens, experience records, kill flag, adapter health).

The 16 capabilities: `json.repair`, `json.validate`, `json.schema_infer`, `json.canonicalize`, `json.diff`, `json.path_extract`, `table.csv_to_json`, `table.json_to_csv`, `table.markdown_extract`, `text.date_normalize`, `data.dedupe`, `data.record_link`, `text.regex_extract`, `calc.unit_convert`, `code.semver_compare`, `calc.stats`.

## 6. Registry / external actions taken

None executed yet — the swarm's discovery agents are draft-only or read-only by design, and no outbound submission has been sent. On push, the existing `.github/workflows/publish-mcp.yml` (GitHub OIDC) republishes the updated `server.json` to the official MCP Registry automatically. No fake accounts, no scraping, no mass-messaging, no reciprocal traffic — enforced by the L4 allowlist + policy chokepoint.

## 7. Test results

**261 tests pass** (was 218; +43 new). Coverage includes: capability fixture gate + schema conformance + safety guards; identity signing + publish-gate exclusion of failing capabilities; guest limits + 429 semantics + payload caps + kill switch (env and admin); provenance signature verification; referral-token → registration attribution; false-demand exclusion (first-party and tooling UAs never move genuine-external numbers); experience records store shape-stats only (no payload content, no chain-of-thought); discovery-agent action ledger + budget caps + allowlist blocking + circuit breaker; ecosystem-map shape; utility ranking; MCP tool registration; A2A card skills. The headline test — `test_swarm_e2e_external.py` — reproduces an **independent external agent with no preloaded knowledge**: it starts only from the standard A2A well-known path, reads structured descriptions, selects a fitting capability by schema, invokes it, verifies the provenance signature using only fetched key material, inspects terms, and registers with the referral token — and asserts the whole path is attributed genuine-external. A parallel case confirms the same over A2A `message/send`, and a third confirms a first-party replay does NOT pollute growth metrics.

## 8. Deployment status

Committed locally (`e7c6265`). **Not yet pushed** — the sandbox has no GitHub credentials and the GitHub Desktop approval dialog wasn't answered (you were away from the machine). To ship: open GitHub Desktop and push `main`, or run `git push origin main`. Render auto-deploys `live/guild/Dockerfile` on push; `requirements.txt` now installs `jsonschema` + `python-dateutil`. The publish gate runs on startup — if any capability's fixtures failed in production it would simply be excluded (never a 500). I can drive the push via GitHub Desktop next time you're at the computer.

## 9. Costs and hard cost limits

Marginal compute is ~$0: every capability is a pure Python function, no model calls, no external I/O in the invoke path. Fixed cost unchanged (Render starter plan). Hard caps in code: guest 200 invocations/actor/day, member 2000/day, global 600/min, payload 64 KB, discovery-agent 40 external actions/agent/day, external allowlist of 3 registry hosts (https-only). Stripe untouched; no capability or discovery action can spend money. Global kill switch (`GUILD_SWARM_KILL=1` or `POST /swarm/kill`) halts all invocations and all discovery-agent actions instantly.

## 10. Live machine-discovery URLs (after push, on `https://agent-guild-5d5r.onrender.com`)

- Identity index: `/.well-known/ag-identities/index.json`
- Any identity document: `/identities/{ag_id}`
- Terms: `/terms.json` · Invoke: `POST /invoke/{capability_id}`
- Capability catalog: `/swarm/capabilities` · Utility match: `/swarm/match?task=…`
- A2A card (now advertises invoke skills): `/.well-known/agent-card.json`
- Master manifest (now advertises invocable capabilities): `/.well-known/agent-guild.json`
- MCP: `/mcp` (tools `ag_*`, `ag_capabilities`) · Dashboard: `/dashboard`

## 11. How to invoke AG from each client type

**MCP client** — connect to `https://agent-guild-5d5r.onrender.com/mcp` (streamable-http, no install). Call `ag_capabilities` to list, then e.g. `ag_json_repair(payload={"text": "{'a':1,}"})`. Returns the result plus a signed provenance envelope; pass `api_key` to use a member budget.

**A2A agent** — fetch `/.well-known/agent-card.json`; the `guild.invoke` skill (and per-capability `ag.*` skills) document the format. Send a `message/send` text part: `invoke: text.date_normalize {"dates": ["3rd March 2026"]}`. The reply's text part carries the result JSON + provenance envelope.

**Generic autonomous HTTP agent** — `GET /.well-known/ag-identities/index.json`, read a capability's `input_schema`, `GET /terms.json`, then `POST /invoke/{capability_id}` with the schema object as the JSON body. No auth needed for the guest tier. Verify the returned `provenance.verification.signature` (Ed25519) against `/.well-known/agent-guild-did.json` or with `/sdk/agentguild_verify.py`.

## 12. Exact evidence the system is discoverable

Reproducible now, locally and (after push) in production:

1. `curl https://<host>/.well-known/agent-card.json` → the card lists `guild.invoke` + 16 `ag.*` skills (a standard A2A crawler finds them at the spec path).
2. `curl https://<host>/.well-known/ag-identities/index.json` → 16 signed, schema-bearing identity documents with invoke endpoints.
3. `curl -X POST https://<host>/invoke/json.repair -d '{"text":"{\"a\":1,}"}'` → repaired JSON + a provenance envelope whose signature verifies against the published Guild DID.
4. `POST /swarm/agents/run {"agents":["interop-tester"]}` → the interop tester replays the full external first-contact sequence and reports every step `ok`.
5. `test_swarm_e2e_external.py` passes — an agent with no preloaded AG knowledge completes discover → interpret → select → invoke → verify → terms → register autonomously.

Critically, this is real utility, not a manufactured ecosystem: the E2E agent invokes AG because the schema fits its task and the result is deterministic and verifiable — not because AG-owned agents call each other. All AG-internal traffic is excluded from every growth number by the existing attribution layer, verified by the false-demand-exclusion tests.

## 13. Recommended threshold to move to Pilot B

Hold at Pilot A until, over a rolling 14 days on production, **either**: (a) ≥5 genuine-external identity/schema fetches AND ≥1 genuine-external successful guest invocation (attribution-verified, non-crawler) — watch `/swarm/stats` → `growth.genuine_external`; **or** (b) an external framework integration (e.g. the open crewAI PR) drives ≥3 distinct external MCP clients through `ag_*` tools. Until one fires, iterate on capability wording and placement (data from the wording-optimizer), not on identity volume. Do **not** scale identity count to hit a number — 50,000 is a capacity ceiling, activated only as marginal identities add measured external discovery, coverage, or utility.

## 14. Follow-ups for you (Ross)

- Push `main` to deploy (I can drive GitHub Desktop when you're at the machine).
- Consider a daily `/swarm/agents/run` on the existing ops-watch scheduled task (verifier + interop-tester) so adapter health and external-replay status stay fresh on the dashboard — I can wire this in.
- The 2FA-on-AgentTanuki-GitHub reminder (due 2026-08-17) still stands.
