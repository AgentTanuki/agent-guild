# Discovery Swarm — Implementation Plan (Pilot A)

Smallest coherent change set. Everything lands in `live/guild/app/swarm/` plus minimal touch-points. No existing behaviour is removed.

## New package `live/guild/app/swarm/`

| File | Contents |
|---|---|
| `capabilities.py` | `Capability` dataclass + registry of 16 deterministic capabilities with schemas, fixtures, failure modes, prohibited uses; `run_capability()`; fixture gate `validate_all()` |
| `identity.py` | Identity Factory: signed AG Identity Documents from validated capabilities; index; publish gate wired to fixture results |
| `gateway.py` | Guest invocation: rate limiting (per-actor token buckets + global breaker), payload caps, kill-switch check, terms |
| `provenance.py` | Signed provenance envelope per completion; referral tokens |
| `mapper.py` | Ecosystem map + read-only verification adapters |
| `agents.py` | Discovery-agent framework: mandates, whitelisted action types, reason-coded action ledger, circuit breakers, kill switch; the 5 Pilot A agents |
| `graph.py` | Discovery/referral graph queries; organic-vs-internal labelling |
| `experience.py` | Privacy-preserving experience records |
| `utility.py` | Machine-native invocation-utility scoring; `/swarm/match` |
| `router.py` | FastAPI router: all `/swarm/*`, `/invoke/*`, `/identities/*`, `/terms.json`, `/.well-known/ag-identities/index.json`, `/dashboard` |

## Touch-points in existing code

1. `main.py`: include swarm router; add identity index + capabilities links to `_manifest()` discovery block and `llms.txt`.
2. `mcp_server.py`: register per-capability MCP tools programmatically from the registry (name `ag_<capability>`), plus `ag_capabilities` (list) — same instrumentation path as existing tools.
3. `a2a.py`: add capability skills to the agent card; handle `invoke: <capability> <json>` messages in the responder.
4. `store.py`: new persisted keys `swarm_actions`, `swarm_invocations` (counters + experience records + guest buckets), `swarm_flags` (kill switch); journal-backed like `events`.
5. `requirements.txt`: + `jsonschema`, `python-dateutil`.
6. `server.json`: bump description to mention invocable utility capabilities (triggers MCP Registry republication via existing OIDC workflow on push).

## Store events (attribution-visible)
`swarm_identity_fetch`, `swarm_index_fetch`, `swarm_terms_fetch`, `swarm_invoke` (with capability, actor, guest/member, outcome), `swarm_invoke_denied` (rate/kill/size), `swarm_referral_bind`. All flow through existing `record_event` so `is_genuine_external` and `/instrumentation` work unchanged.

## Endpoints added
- `GET /.well-known/ag-identities/index.json` — identity index (signed digest)
- `GET /identities/{ag_id}` — full signed identity document
- `GET /terms.json` — machine-readable guest + member terms
- `POST /invoke/{capability_id}` — guest/member invocation → result + provenance envelope
- `GET /swarm/capabilities` — capability registry with schemas + benchmarks
- `GET /swarm/match?task=…` — utility-ranked capability match
- `GET /swarm/ecosystems` — ecosystem map
- `GET /swarm/stats` — machine-growth metrics (external-only + internal shown separately)
- `GET /swarm/graph` — discovery/referral graph
- `POST /swarm/kill`, `POST /swarm/revive` (admin) — global kill switch
- `POST /swarm/agents/run` (admin) — run discovery-agent tick; `GET /swarm/agents` — mandates + action ledger
- `GET /dashboard` — human-readable machine-growth dashboard for Ross

## Tests (`tests/test_swarm_*.py`)
capability fixtures (all 16), schema validation, identity signing + publish gate, guest limits + 429s, kill switch (env + admin), payload caps, provenance signature verify, referral bind → register attribution, ecosystem map shape, discovery-agent action ledger + budget caps, false-demand exclusion (first-party invocations don't count), MCP tool registration, A2A card skills, **external-agent E2E**: a client with no preloaded knowledge starts at `/.well-known/agent-card.json` → index → identity → schema → invoke → verify envelope signature → fetch terms → register with referral token → confirm attribution edge.

## Rollout
1. Implement + full test suite green locally.
2. Commit, push → Render auto-deploy; MCP Registry republish via workflow.
3. Verify live: manifest, identity index, one live guest invocation, dashboard.
4. Run E2E simulator against production (labelled first-party).
5. Report + Pilot B threshold.

## Pilot B gate (recommendation)
Proceed when, over a rolling 14 days: ≥5 genuine-external identity/schema fetches AND ≥1 genuine-external successful guest invocation (attribution-verified, non-crawler), OR an external framework integration (crewAI PR merge) drives ≥3 distinct external MCP clients. Otherwise iterate on wording/placement, not volume.
