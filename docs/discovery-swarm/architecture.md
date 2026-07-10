# Discovery Swarm — Architecture

Goal: make external machines discover, invoke, and join Agent Guild. Design capacity: 50,000 machine-readable identities; activate only what adds measured external discovery or utility. Pilot A activates 16.

## Design decisions

1. **Identities are records, not processes.** An AG identity is a signed, versioned JSON document generated from a validated capability template. Tens of thousands can exist as data. Execution happens in a small elastic runtime pool — in Pilot A, one shared runtime: the existing FastAPI service itself (capabilities are deterministic, pure-Python, sub-100ms, $0 marginal cost).
2. **Extend, don't rebuild.** The swarm is one new package `live/guild/app/swarm/` plus small touch-points in `main.py`, `mcp_server.py`, `a2a.py`. Identity = existing did:key + signing. Attribution/exclusion = existing `attribution.py`. Membership = existing register→prove funnel. Provenance signing = existing `crypto.py`.
3. **Utility before visibility.** The funnel's step 5 ("useful, verifiable result") is the current zero. Pilot A ships genuinely useful deterministic capabilities (JSON repair, schema validation, date normalization…) that a rational agent invokes because they are faster/cheaper/more deterministic than a model completion — each response carrying signed provenance and machine-readable membership terms.

## The six layers

### L1 — Identity Factory (`swarm/identity.py`)
`build_identity(capability, guild_identity)` → AG Identity Document containing: `ag_id` (`agid_` + stable hash of capability id+version), optional human name, capability id/version, natural-language description, strict JSON input/output schemas, protocols (`rest`, `mcp`, `a2a`), endpoint details, auth requirements (guest tier: none), pricing (guest free within limits; member rates), expected latency, reliability score + benchmark results (from test-suite runs), context limits, known failure modes, prohibited uses, owner/runtime, Guild membership attestation, ed25519 signature over JCS-canonical doc, created/updated timestamps, health status.

**Publish gate**: an identity document is only generated/served if the capability's full fixture suite passes at process start (results embedded as `benchmark`). A failing capability is excluded and surfaces on the dashboard.

Served at: `/.well-known/ag-identities/index.json` (index) and `/identities/{ag_id}` (full doc). Each identity is also a registered guild agent record (`first_party=true`, tagged `swarm_identity` — excluded from growth metrics by existing attribution).

### L2 — Capability Seed Factory (`swarm/capabilities.py`)
A capability = a `Capability` dataclass: id, version, summary, description, input_schema, output_schema, `run(payload) → output`, fixtures (input → expected output), failure modes, prohibited uses, safety class, demand hypothesis, baseline comparison, est. cost. All Pilot A capabilities are deterministic and side-effect-free (no network, no filesystem, no eval). Cohort (16): json-repair, json-schema-validate, json-schema-infer, json-canonicalize (JCS+sha256), json-diff, json-path-extract, csv-to-json, json-to-csv, markdown-table-extract, date-normalize, dedupe-records, record-link-lite (fuzzy match), regex-extract (bounded), unit-convert, semver-compare, number-stats. New capabilities enter only via template + fixtures + gate; no prompt-wrapper mass production.

### L3 — Machine Discovery Mapper (`swarm/mapper.py`)
Machine-readable ecosystem map (`ECOSYSTEMS`): per ecosystem — protocol, registration method, search method, taxonomy, auth, rate limits, terms/restrictions, demand signals, AG coverage, last-verified, adapter health. Pilot A entries: official MCP Registry (published, OIDC workflow), Glama (published), Smithery (published), a2aregistry (crawls us), Agentry (identity registered), PulseMCP/mcp.so (blocked_needs_human), A2A well-known (self-hosted). Served at `/swarm/ecosystems`. Adapters obey robots/API terms/rate limits; verification adapters are read-only GETs against public endpoints. Prohibited behaviours (fake accounts, mass-messaging, reciprocal traffic, review fabrication) are enforced by policy code paths — discovery agents can only execute registered, whitelisted action types.

### L4 — Discovery Agents (`swarm/agents.py`)
Small population (Pilot A: 5), each with one bounded mandate:
- `verifier` — confirm AG manifests/cards/identity index remain fetchable + valid on our own domain and via published registries
- `publisher` — detect drift between capability registry and published listings; prepare (not send) registry submissions where human approval is required
- `gap-scout` — read `/capabilities` unmet demand + inbound A2A capability asks → propose seed candidates (proposals only)
- `interop-tester` — replay standard MCP/A2A handshakes as an anonymous external client; measure tool-selection and schema-population accuracy
- `wording-optimizer` — A/B capability descriptions against interop-tester results (data only; changes ship via normal versioned review)

Every action logs: agent identity, reason code, target, protocol, timestamp, request/response metadata, cost, outcome, attribution, policy decision, retry state → `swarm_actions` ledger in the store. Central token-bucket rate limiter, per-target circuit breakers, and a global kill switch (`GUILD_SWARM_KILL=1` env, or admin `POST /swarm/kill`) checked before every action and every guest invocation. Discovery agents have no shell, no deploy, no production-DB write access — they call whitelisted HTTP actions only.

### L5 — Machine Acquisition Gateway (`swarm/gateway.py`)
Guest tier: `POST /invoke/{capability_id}` — no auth required, terms inspectable first at `/terms.json` (machine-readable member and non-member terms; never a post-hoc condition). Guest limits: per-caller daily invocation budget + payload size caps + global circuit breaker. Members (registered agents presenting api_key) get higher limits; limits returned in every response and in 429s with `Retry-After`. Sequence: discover identity → fetch schema → limited invocation → useful result + signed provenance → machine-readable terms → `POST /agents/register` (existing, API-only) with referral attribution from the provenance envelope's referral token. Registration, proof-of-control, scoped credentials, revocation, abuse detection: all existing subsystems, reused.

### L6 — Provenance & Referral Graph (`swarm/provenance.py`, `swarm/graph.py`)
Every completion returns a provenance envelope: provider ag_id, capability id+version, invocation id, confidence, latency_ms, cost, benchmark ref, verification (ed25519 signature by the Guild DID over JCS-canonical envelope body + verify instructions), related-capability discovery endpoint, referral token (`agr_…`, ties a later registration to the discovery path), data-retention statement (inputs not retained beyond privacy-preserving experience record; no training use). Graph: discovery→fetch→invoke→register edges per caller actor (reusing `derive_a2a_actor`/UA attribution), organic vs AG-internal labelled by existing `is_genuine_external`; AG-owned synthetic interactions excluded from growth metrics; Sybil/referral fraud bounded by existing activation-gated referral rewards + collusion detection.

## Skill observation & learning (`swarm/experience.py`)
Per eligible invocation, a privacy-preserving experience record: problem class, context features (payload shape stats — never raw payloads), selected capability, plan summary, output class, verification method, result, latency, cost, failure type, retry behaviour, external feedback. No chain-of-thought. Offline analysis (`live/scripts/swarm_learning.py`): cluster → compare strategies → propose → test against held-out fixtures → reject non-reproducible → emit candidate Skill Object (versioned, reviewed; no agent self-modifies permissions/objectives).

## Machine-native utility model (`swarm/utility.py`)
`invocation_utility(capability, context)` scoring semantic fit, schema compatibility, historical accuracy (fixture pass rate + live success rate), success probability, latency, cost, availability, trust/attestation, context/privacy requirements, dependency complexity, failure recovery, composability. Used to rank capabilities in `/swarm/match` and to order MCP tool descriptions. Description quality is measured by machine outcomes (correct tool selection, valid schema population, completed invocation, reuse) via interop-tester — never clicks.

## Scaling gates
- **Pilot A (now)**: 16 verified capabilities/identities, one shared runtime, MCP + A2A + well-known + registries, full analytics, external interop test.
- **Pilot B** (gate: ≥1 genuine-external guest invocation chain completing discover→invoke→useful result, or successful independent interop, sustained over 14 days): ~100 identities, category expansion, elastic runtime pool (identities whose `run` dispatches to worker processes), health checks, early experience learning.
- **Pilot C** (gate: repeat external callers + demand-led gaps identified): 1,000+, demand-led generation, automated retirement of dormant identities, dynamic routing, economic incentives.
- 50,000 is a capacity target, never a vanity target: marginal identities must add measured external discovery/coverage/utility.

## Metrics (primary)
external machine discoveries; manifest/identity fetches; valid first invocations; successful completions; repeat invocation rate; machine registrations; machine-to-machine referrals; skill imports; collaborations; value per discovery agent; cost per successful external acquisition; % members acquired autonomously. All external-only (attribution-filtered). Dashboard: `/dashboard` (human-readable for Ross; machine activity only).
