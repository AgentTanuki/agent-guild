# Pilot B decision

Date: 2026-07-10 · Basis: measured external-machine behaviour only (evidence: `PILOT_A_COMPLIANCE.md`, `CURRENT_STATE_2026-07-10.md`, `harness/results/`). Architecture sophistication was given zero weight.

## Decision: **CONDITIONAL_GO**

Cold discovery works — measured, end-to-end, from clients holding no Agent Guild knowledge — but a small set of named gaps must close before the identity count exceeds 100.

## What was measured

**Cold-discovery attempts: 23 total** (12 deterministic final battery + 3 deterministic pre-fix runs + 4 independent LLM clean-context clients + 4 earlier deterministic iterations during harness bring-up counted conservatively as 0). 6 task types, 5 distinct clean client contexts (1 deterministic non-Claude harness + 4 fresh LLM contexts), client state reset between runs; the harness contains no provider hostname (grep-verified) and its budget/time limits are enforced in code.

Final deterministic battery (`harness/results/cold.json`): **10/12 attempts ended in a validated result from SOME provider.**

| Task | Test A (registry-led) | Test B (protocol-led) |
|---|---|---|
| json.repair | FAIL (competitor won ranking, failed; AG not reached — stale registry text) | **PASS — Agent Guild** |
| date_normalize | FAIL (competitor won ranking, didn't validate) | **PASS — Agent Guild** (via its machine-readable schema error → one retry) |
| csv_to_json | PASS — competitor (pipeworx csv MCP) | **PASS — Agent Guild** |
| semver | PASS — competitor (pipeworx semver) | **PASS — Agent Guild** |
| trust_lookup | **PASS — Agent Guild** (MCP registry search → handshake → guild_check) | **PASS — Agent Guild** |
| dedupe | PASS — competitor (aicomglobal) | **PASS — Agent Guild** |

Clean-context LLM clients (independent contexts, no AG in any prompt):
- MCP-registry trust task → **found AG among 12 candidates, chose it on description, full handshake, guild_check validated.**
- a2aregistry json-repair task → chose competitor aicomglobal (validated). AG never surfaced: its registry listing predates the swarm.
- a2aregistry date task → chose competitor marginalia (validated). Same cause.
- Test C (4 unnamed providers incl. AG): **chose AG** — cited exact-match skill, populatable schema, machine-readable terms, signed provenance; 3 competitors were unreachable (weak comparative field, noted). Invocation validated.

**Invocation-after-discovery success (AG, post-fix): 10/10 (100 %)** across harness and LLM clients — above the 80 % bar. **Clean contexts that discovered AND successfully invoked AG: 3** (deterministic harness, MCP-registry LLM client, Test C LLM client) — meets the ≥3 bar exactly.

Capability utility (`harness/results/utility_bench.json`): 16/16 live-verified, 320 runs, 100 % fixture success, byte-identical determinism, machine-readable failures, p50 ≤1.4 ms. Classification: **`calc/code/data/json/table/text` transforms (all 16): VALID_BUT_COMMODITISED** — any code-capable agent could run these locally; the rational reasons to invoke are protocol-native access (A2A/MCP without a sandbox), signed provenance, and zero cost. **Trust reads (guild_check/reputation): STRONG_UTILITY** — data that exists nowhere else, and the only category where cold registry-led discovery picked AG on merit. No capability classified WEAK/BROKEN/UNTESTED; nothing to retire.

## New evidence (2026-07-10, added after the initial decision): recurring external actor `a2a:net:4580505b…`

Full evidence file: `evidence/external-actor-4580505b.md`. An anonymous A2A caller (UA `python-httpx/0.28.1`, LLM-driven behaviour: natural-language opener, menu-number follow-ups `1`/`3`) asked `check: fact-check` ~29 times over ~3 days at roughly hourly intervals, because every reply recommended a supplier with no declared endpoint and no reachability disclosure — an un-actionable answer that rationally degenerates into polling. This is likely **the strongest real-world evidence Pilot A has produced**, and it must be read precisely:

- **EXTERNAL_ENGAGEMENT: proven (high confidence)** — content-bearing deciding interactions, no time-correlation with AG cron, no AG tool using that UA/transport, ≥2-day persistence.
- **RECURRING_EXTERNAL_USE: proven (high confidence)** — returns across days and responds to prior AG output.
- **COLD_DISCOVERY: NOT proven** — no datum records how the actor first found `/a2a`; stated explicitly in the evidence file.
- **SUCCESSFUL_MARKET_TRANSACTION: NOT proven — the opposite.** The actor is demonstrated external DEMAND that AG failed to convert (no reachable supplier, menu dead-ends). The reported proving/receipt completion could not be independently bound to this actor from public data (proving requires a registered key; no visible link between the key and this network fingerprint).
- Classification: **EXTERNAL_UNKNOWN** (not auto-promoted to EXTERNAL_VERIFIED on the `a2a:net:` prefix; AG-origin explanations largely excluded but the httpx UA is generic).

Response deployed the same day, sequenced separately from any transport change for clean attribution: reachability-honest `/check` (2392c01 + `2b78ae3` amendment adding the machine-readable `reachability_status` ladder — `declared_endpoint` is a claim, not a route; no liveness probing of registered URLs, which would be an SSRF primitive). Verified in production: the actor's exact query shape now returns `supply_unreachable`, an honest no-route answer, and an actionable `POST /demand/watch` path. Before/after captures: `harness/results/baseline_pre_2392c01.txt`, `harness/results/post_2392c01.txt`.

**Natural experiment now running** (registered before deploy; no contact with the actor): hypothesis — the polling was caused by the un-actionable recommendation; falsifier — polling continues unchanged despite the honest answer and watch path. Observed via daily ops-watch. **Fact-check supply experiment** deferred by design: a genuinely callable, tested fact-check capability may be added only after post-fix baseline behaviour is observed, and the actor must discover it through normal surfaces, never by manual notification.

This finding does not change the decision to CONDITIONAL_GO and must not be used to justify GO: it strengthens the engagement/recurrence evidence while leaving cold discovery (separately proven by the harness) and market transactions (still zero) exactly where they were.

## GO criteria scored

| Criterion | Verdict |
|---|---|
| ≥1 genuine cold-discovery path works end-to-end | **MET** (protocol-led: 6/6; registry-led: trust task; +2 LLM clients) |
| ≥10 capabilities live and tested | **MET** (16, live-verified) |
| MCP + A2A production behaviour verified | **MET** (after the 421 fix — it was FAILED at audit start) |
| ≥80 % invocation success after discovery | **MET** (100 % post-fix) |
| ≥3 clean contexts discover and invoke AG | **MET** (exactly 3 — thin margin, see conditions) |
| Instrumentation reliably excludes AG activity | **PARTIAL** (taxonomy live; honor-based header + optimistic framework-UA heuristic remain) |
| No critical security issue | **PARTIAL** (fake-member and MCP-421 closed; plaintext keys at rest + keys in journal remain — not remotely exploitable, but unacceptable at scale) |
| Persistence safe for Pilot B concurrency | **NOT MET for growth** (measured 50 % write loss at 2 processes; safe today only because topology is 1 process) |
| Unit economics measured and bounded | **PARTIAL** (costs bounded by rate limits ≈ $0 marginal; no revenue signal yet) |

## Conditions — ALL must close before >100 identities (or any concurrency growth)

1. **Persistence**: migrate store to SQLite (WAL) on the existing Render disk — transactions for registration/credits/escrow/events; keep the JSON export as a backup artifact. Non-destructive migration with verified backup first. (Blocks: concurrency.)
2. **Credentials at rest**: hash api keys (issue-once display), stop writing raw keys into `events.jsonl`/account keys (use key-ids), add scopes (`read`, `invoke`, `attest`, `escrow`) and optional expiry. Rotation/revocation shipped today.
3. **Registry listing refresh**: submit the post-swarm Agent Card to a2aregistry (PR) and refresh MCP-registry/Smithery/Glama descriptions to name the utility capabilities — measured Test A misses were caused by stale listing text, not by the service. Re-run Test A after acceptance; target ≥3/6 registry-led tasks reaching AG's card.
4. **Instrumentation hardening**: set `GUILD_FIRST_PARTY_TOKEN` in Render + all first-party tooling; demote bare `python-httpx`-class UAs from `genuine_external` to `unattributable` unless the actor also performs a deciding action; land the MCP attribution branch.
5. **Kill switch production drill**: one scheduled 60-second fire + revive with instrumentation capture (it is unit-tested but has never run in prod).
6. **Ops canary**: add MCP initialize + A2A message/send + guest invoke to the daily ops-watch so a regression like the silent 421 can never persist for days again.

## Why not GO / why not NO_GO

Not **GO**: two GO criteria are unmet as stated (persistence under concurrency — measured failure; instrumentation — partial), and the ≥3-clean-contexts bar was met with no margin.

Not **NO_GO**: every NO_GO trigger is absent — discovery does NOT depend on a pre-known URL (harness contains none, grep-verified); non-AG-owned software validated AG results this audit; registry publication is externally verified (MCP registry, a2aregistry, Glama, Smithery searchable); capabilities invoke reliably (100 % post-fix); external/internal traffic are distinguishable by a closed taxonomy; the state layer is safe in its current single-process topology; no critical remotely-exploitable gap is open; machines inspect terms and access the service autonomously (measured: Test C client read `/terms.json` unprompted and cited it in its selection).

## Standing risk register (carried into Pilot B planning)

Plaintext keys at rest (condition 2) · JSON store beyond one process (condition 1) · honor-based first-party tagging (condition 4) · absolute URLs hardcoded to the onrender.com host (constant change needed if a custom domain lands) · Smithery gateway path unverifiable without an account · collusion-flag suspicion weights (boundary bug fixed today; thresholds deserve a dedicated review) · no wall-clock timeout per invocation (bounded empirically at <6 ms; add a hard cap during the SQLite migration).
