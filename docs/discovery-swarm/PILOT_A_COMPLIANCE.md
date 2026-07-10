# Pilot A compliance matrix

Date: 2026-07-10 · Production: `https://agent-guild-5d5r.onrender.com` · Evidence files: `docs/discovery-swarm/harness/results/` · Companion: `CURRENT_STATE_2026-07-10.md`, `PILOT_B_DECISION.md`.

Statuses reflect the state AFTER the fixes deployed during this audit (`e8749bd`, `7c4b6f0`, `f41362d`) and full-suite reruns (275/275 tests pass). "At audit start" notes record what was true before.

Score: **PASS 34 · PARTIAL 9 · FAIL 4 · NOT_TESTED 3** (50 items).

## 1. Identity and capability quality

| Item | Status | Evidence / test | Remediation |
|---|---|---|---|
| 10–20 narrow, non-duplicative capability identities | **PASS** | 16 identities live (`/.well-known/ag-identities/index.json` returns count 16, verified in prod). Distinct transforms, no duplicates (capabilities.py:643–1105). | — |
| Stable identifier + version per capability | **PASS** | `id` + semver `version` on every capability (e.g. `json.repair` 1.0.0); `agid_*` per identity; verified in index + invoke responses. | — |
| Strict machine-readable input/output schemas | **PASS** | JSON Schema draft 2020-12 per capability, enforced at the single chokepoint (`run_capability`, jsonschema; `additionalProperties:false`). Live test: bad payload → 200 with `schema_validation` error object carrying the schema. | — |
| Meaningful test suite per capability | **PASS** | Inline fixtures per capability incl. `expect_error` cases; boot-time publish gate (identity served only if fixtures pass); tests/test_swarm_*.py; 320-run bench: 100 % fixture success (`harness/results/utility_bench.json`). | — |
| Limitations + failure conditions declared | **PASS** | `failure_modes` + `prohibited_uses` tuples on all 16 (audited; counts in utility_bench.json). | — |
| Published benchmark claims reproducible | **PARTIAL** | Identity docs cite benchmark URLs; fixture-derived numbers reproduce locally. No INDEPENDENT reproduction path documented beyond fixtures. | Publish the bench harness alongside the identity doc. |
| No capability is an untested prompt wrapper | **PASS** | All 16 are deterministic code (no network/LLM/eval — module docstring + code audit); determinism re-verified: identical payloads across double-runs on all 16. | — |
| Verifiable AG membership/provenance per identity | **PASS** | Identity docs are Ed25519-signed; every invocation returns a signed `ag-provenance/1` envelope (verified live: signature + signer DID + verifier scripts at `/sdk/agentguild_verify.py|.mjs`). | — |

## 2. MCP

| Item | Status | Evidence / test | Remediation |
|---|---|---|---|
| Production endpoint completes initialize handshake | **PASS** (was **FAIL**) | At audit start: `421 Misdirected Request` on every request (unpinned fastmcp Host guard). Fixed e8749bd; verified: initialize → 200, protocol 2025-03-26, both `/mcp` and `/mcp/`. | Keep fastmcp pinned; add an external MCP-handshake canary to ops-watch. |
| Tool discovery from a standards-compliant external client | **PASS** | Clean-context client (no AG knowledge) found AG in the MCP registry, completed initialize → notifications/initialized → tools/list (30 tools) → tools/call guild_check. Deterministic harness independently exercised the same path. | — |
| Every advertised tool invocable or explains why not | **PARTIAL** | guild_check, guild_search, ag_* (16) verified across harness/tests. Write tools (guild_attest, guild_escrow_*) require a valid key and return machine-readable auth errors — verified in unit tests, not invoked in prod during this audit. | Prod smoke-test of authed MCP writes. |
| Tool schemas match accepted inputs | **PASS** | ag_* tools reuse the capability schemas enforced server-side; guild_* signatures typed. Harness populated calls purely from declared schemas and succeeded. | — |
| Invalid inputs → machine-readable errors | **PASS** | Verified live: invalid payload returns structured `{error, message, path, input_schema}`. | — |
| Auth + guest behaviour documented and testable | **PASS** | `/terms.json` (guest vs member tiers, limits, retention) + tool descriptions; guest MCP calls work without credentials. | — |
| MCP calls correctly identified in instrumentation | **PARTIAL** | MCP clients tagged `mcp:<clientInfo.name>`; branch `mcp-attribution-fix` exists for a known funnel blind spot; today's audit client had to be manually registered as a first-party incident — self-identification is spoofable both directions. | Land mcp-attribution-fix; consider handshake-bound actor keys. |

## 3. A2A

| Item | Status | Evidence / test |
|---|---|---|
| Agent Card available in production | **PASS** — `/.well-known/agent-card.json` 200 (+ legacy `/agent.json`). |
| Card conforms to the A2A spec version used | **PASS** — `protocolVersion 0.3.0`, JSONRPC transport, capabilities flags, skills with modes/examples; accepted by the a2aregistry validator (listed + healthy) and by 3 independent clients this audit. |
| Skills/endpoint declarations match production behaviour | **PASS** (was **PARTIAL**) — 16 `ag.*` skills + `guild.invoke` now carry fully-formed examples built from real fixtures (7c4b6f0); cold-discovery clients invoked from card metadata alone, successfully. |
| External A2A client completes a JSON-RPC request | **PASS** — verified by harness + 2 clean-context clients + Test C client (message/send, invoke, check flows). |
| Unsupported methods → correct structured errors | **PASS** — `tasks/get` → −32601 with hint; parse/invalid-request/params errors verified in tests (a2a.py:405–439). |
| Auth requirements machine-readable | **PASS** — card + terms.json declare guest (none) and member (X-API-Key) clearly. |
| Badge/membership claims verifiable, not self-asserted | **PASS** — provenance envelopes signed by the Guild DID (did doc served at `/.well-known/agent-guild-did.json`); `POST /credentials/verify` + downloadable verifiers. |

## 4. Generic machine access

| Item | Status | Evidence |
|---|---|---|
| Generic HTTP client discovers endpoints without human docs | **PASS** — content-negotiated `/`, `/llms.txt`, `/.well-known/*`, OpenAPI; harness + subagents never read human pages. |
| OpenAPI accurate | **PASS** — local vs prod parity after deploy; invoked routes behaved per schema. |
| `/capabilities` `/search` `/check` `/standard` consistent | **PASS** — spot-verified; all generated from the same store/capability registry. |
| Pricing/auth/constraints inspectable BEFORE sending data | **PASS** — `/terms.json` (schema `ag-terms/1`): tiers, limits, cost, retention, kill-switch disclosure. Test C client cited it as a deciding factor. |
| Machine registration route exists | **PASS** — `POST /agents/register`, no browser, returns key once; exercised live (agent_2aab7b84f22c). |
| Credential issued, scoped, tested, revoked | **PARTIAL** (was **FAIL**) — issue+test+revoke+rotate now live (f41362d, verified in prod after deploy). **Scopes and expiry still do not exist.** |

## 5. Provenance and attribution

| Item | Status | Evidence |
|---|---|---|
| Signed provenance on successful responses | **PASS** — Ed25519 over JCS, schema `ag-provenance/1`, verified live incl. by an independent clean-context client. |
| Provider identity/capability version/latency/status consistent | **PASS** — envelope fields consistent across REST/A2A/MCP (same gateway chokepoint). |
| External callers can distinguish AG metadata from model output | **PASS** — deterministic providers + signed envelope; `verification.over = JCS(envelope)`. |
| Referral attribution not trivially forgeable | **PARTIAL** — referral tokens are unguessable (`agr_<hex>`) and DAG-constrained, but a referrer can self-report volume by minting guest invocations (rate-capped). Not load-bearing for money today. |
| AG-owned synthetic traffic excluded from external growth | **PARTIAL** (see §7) — honor-based header + heuristics + incidents; caller-class gate added today. |
| `genuine_external_engaged` precisely defined + tested | **PASS** — definition in `/instrumentation` note; deciding/probe/surfacing split; tests (test_attribution.py, test_a2a_actor_attribution.py, new test_caller_classes.py). |

## 6. Safety and operations

| Item | Status | Evidence / remediation |
|---|---|---|
| External inputs treated as untrusted | **PASS** — schema validation at chokepoint, payload cap 64 KB, `additionalProperties:false`. |
| Prompt injection tested | **PASS** (scoped) — no LLM sits behind any capability (deterministic code), so the classic injection surface is absent; A2A text parsing is regex-based; malformed `invoke:` now returns a corrective error (tested). |
| SSRF protections where URLs are supplied | **PASS** — agent-declared endpoints stored but never fetched; discovery agents fetch only an https allowlist (3 registry hosts) with timeouts. |
| Recursive/unbounded delegation prevented | **PARTIAL** — self-dealing blocked (task/attest/escrow), referral DAG-by-construction; no general delegation-depth counter (nothing recurses today). |
| Per-invocation time/spend limits | **PARTIAL** — spend: guest free, member metered, escrow bounded by balance; payload+internal caps (250 k comparisons, ReDoS guard, ≤10 k values); **no wall-clock timeout per invocation** (bounded in practice: p50 <1.5 ms, max 5.6 ms across 320 runs). |
| Central rate limits | **PARTIAL** — swarm gateway fully limited; legacy REST reads/writes rely on billing metering only. |
| Global kill switch exists and tested | **PARTIAL** — exists, persists, enforced at chokepoints; unit-tested (test_swarm_gateway.py). **Not fired against production** (deliberate). |
| Agent credentials scoped and revocable | **PARTIAL** — revocable+rotatable as of today (verified live); **no scopes/expiry**. |
| Production writes auditable | **PASS** — append-only events journal + durable ledger chain + published checkpoints. |
| JSON persistence: documented concurrency/corruption strategy | **PARTIAL** — atomic-rename + journal + single-worker topology documented; **no fsync; unsafe beyond one process (measured: 50 % write loss with 2 processes)**. |
| Multiple runtime instances can't silently overwrite state | **FAIL** — measured lost updates + a crash in the 2-process test. Mitigated today only by single-instance topology (Render disk prevents horizontal scaling). Migration required before any concurrency growth. |

## 7. Analytics

| Item | Status | Evidence |
|---|---|---|
| Manifest fetches distinguishable from invocations | **PASS** — `swarm_index_fetch`/`swarm_identity_fetch`/`swarm_terms_fetch` vs `swarm_invoke` (verified in prod events). |
| Internal / test / external callers distinguishable | **PARTIAL** — 7-class taxonomy live as of today (`caller_classes` in `/instrumentation`); residual weaknesses: honor-based first-party header (no `GUILD_FIRST_PARTY_TOKEN` set), generic framework UAs (httpx) count as genuine, incidents are manual. |
| First external invocation measurable | **PASS** — `first_genuine_external_at` + per-actor keys. |
| Repeat external invocation measurable | **PASS** — repeat_query/strong-actor logic (de6e436) + per-caller A2A keys. |
| Failed discovery and failed invocation measurable | **PARTIAL** — `swarm_invoke_denied` + error_kind in provenance cover invocation failure; **failed discovery** (search that never reaches AG) is unobservable server-side by nature; registry-side analytics absent. |
| Registration source + referral chain measurable | **PASS** — `referred_by`, referral edges, `discovery` block in provenance, registration events. |
| Costs attributable to identity/capability | **PASS** — per-invocation `cost_credits` + billing log per account; guest costs zero by design. |
| Metrics can't be inflated by AG-owned agents calling each other | **PARTIAL** — first-party accounts excluded; seed traffic tagged; fake-member hole closed today; residual: an AG-owned agent omitting the header still needs manual incident tagging. |

## 8. FAIL / NOT_TESTED register (complete)

- **FAIL — multi-process persistence** (measured data loss). Blocker for concurrency growth; see PILOT_B_DECISION conditions.
- **FAIL — credential scopes/expiry absent** (rotation/revocation now exist; scopes don't).
- **FAIL — api keys plaintext at rest + raw keys in events journal/accounts file.**
- **FAIL — registry listing content stale** (a2aregistry + Smithery tool lists predate the swarm; data-task registry-led discovery misses AG at the text stage — measured in Test A).
- **NOT_TESTED — kill switch in production** (unit-tested only; firing it would interrupt live surface).
- **NOT_TESTED — Smithery gateway invocation end-to-end** (requires a Smithery account key).
- **NOT_TESTED — Stripe billing path** (no live key configured; dev-token path only).
