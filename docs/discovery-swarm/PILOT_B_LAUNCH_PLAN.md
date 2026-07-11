# Pilot B launch plan

2026-07-11 · **Do not launch the swarm yet.** Pilot B begins only after the `PILOT_A_CLOSEOUT.md` §F gate is met. This plan defines a *controlled, evidence-led* first cohort — explicitly **not** a jump to tens of thousands of live runtimes. It inherits the CONSTITUTION and machine-economics lens: infrastructure, not features; the customer is a rational zero-loyalty agent.

## Guiding principle

Scale only what has produced evidence. Pilot A ended with **one recurring external caller** and **zero completed transactions**, so the first cohort's job is to produce the *first* clean registry-led cold-discovery + a first genuine external invocation — not volume.

## 1. First controlled cohort

- **Size:** ≤ 10 published capability identities (well under the CONDITIONAL-GO ceiling of 100). Start at the current 16 utility capabilities already live; do NOT add identities until one registry-led cold-discovery succeeds.
- **Identity-to-runtime ratio:** 1:1 — every published identity maps to a real, invocable, fixture-gated capability. **No identity without a working runtime behind it** (the Pilot A rule: no untested prompt wrappers, no numeric inflation).
- **Capability categories:** the existing deterministic utilities (JSON/CSV/date/semver/dedupe/stats — `VALID_BUT_COMMODITISED`) plus, when its gate opens, the evidence-relative `evidence.claim_check` (`STRONG-ish` only if it earns invocation). Trust reads (`guild_check`) remain the `STRONG_UTILITY` anchor.

## 2. Discovery surfaces

MCP registry, a2aregistry, Glama, Smithery — **only after** their listings are refreshed and independently indexed (Step 5). No new surfaces until the existing ones are verified current.

## 3. Machine-only acquisition flow

The full path must work with **zero human step**: discover → read `/terms.json` → guest invoke → register (machine) → receive scoped credential → member invoke → rotate/revoke. Pilot B measures how far real external agents get through this unaided (the north star).

## 4. First-party traffic controls

`GUILD_FIRST_PARTY_TOKEN` set and enforced; every AG-owned job/canary/probe classifies `AG_INTERNAL`/`AG_TEST`; the central analytics invariant keeps them out of growth metrics. **No cohort metric counts until first-party classification is verified clean** (per `PILOT_A_METRICS_BASELINE.md`).

## 5. Swarm kill switch

`POST /swarm/kill` (admin) — drilled: external invocation → 503, read-only discovery survives, operator access intact, auditable, reversible. Must be confirmed working in the target backend before any discovery-agent activity.

## 6. Cost limits (bounded, no material third-party charges)

- Guest 200 invocations/actor/day; member 2000; global 600/min; 64 KB payloads.
- Reachability probes: capped concurrency (`GUILD_REACH_MAX_PROBES`), 3 s timeout, per-agent rate-limited.
- No paid external API in a capability without an explicit cost review. Escrow/credits remain simulated (no real money).
- Per-invocation compute bounded (deterministic capabilities, p50 < 2 ms).

## 7. Success metrics (measured against the clean baseline)

- ≥ 1 **registry-led** cold-discovery path succeeds end to end (the gate item).
- First genuine external **invocation** (not just discovery) — `INVOCATION_ATTEMPTED → SUCCEEDED → RESULT_VALIDATED`.
- First **repeat** external invocation by a validated external caller.
- First machine-only **registration → member invocation** with a scoped credential.
- Instrumentation remains contamination-free (AG_* excluded, verified).

## 8. Failure metrics (watch actively)

- Failed discovery (search that never reaches AG) — inferred from registry-side + cold-discovery test misses.
- Failed invocation after discovery (< 80 % success post-discovery = regression).
- Any AG-owned traffic appearing in genuine-external (attribution leak — page immediately).
- Persistence anomaly (lost update, corrupt state, integrity_check fail).
- The `4580505b` actor returning to the old polling pattern despite the fix (natural-experiment falsifier).

## 9. Scale gates (do not cross without evidence)

- **Gate A → beyond 16 identities:** one registry-led cold-discovery success + stable backend + clean attribution.
- **Gate B → beyond 100 identities:** SQLite cutover complete and observed stable, credential hashing activated-or-explicitly-deferred, ≥ 3 clean external cold-discovery contexts, ≥ 1 genuine external invocation validated.
- **Gate C → multi-instance / >1 worker:** **STOP — migrate to managed Postgres first** (a Render disk cannot be shared; the SQLite startup guard refuses multi-worker). No horizontal scale on SQLite, ever.

## 10. Stop conditions (halt Pilot B, do not push through)

- A foundational-defect regression (identity/attribution/persistence/credentials/reachability/ledger) — reopen Pilot A per closeout §G.
- Attribution contamination that inflates external growth and cannot be immediately corrected.
- Any credential secret found at rest in logs/journal/events after hashing activation.
- Persistence write-loss or corruption under the live topology.
- A material unbudgeted third-party charge.

## 11. Rollback plan

- Config activations are individually reversible: unset the flag + redeploy (`GUILD_FIRST_PARTY_TOKEN`, `GUILD_STORE`, `GUILD_HASH_KEYS`, `GUILD_ENABLE_CLAIMCHECK`).
- SQLite rollback = `GUILD_STORE=json` (mind post-cutover writes — keep the window short; no reverse-migration script exists).
- Credential hashing is one-way → restore the pre-migration backup.
- Swarm kill switch halts discovery-agent activity instantly.
- The architecture baseline `a9d0380` is the frozen return point.

## 12. Fact-check supplier — gated

Publish `evidence.claim_check` (`GUILD_ENABLE_CLAIMCHECK=1`) **only after** the `4580505b` natural experiment reaches its pre-recorded observation point and the supplier passes its own utility+safety tests with a declared, verified endpoint. Success sequence = external demand → demand recorded → reachable supply added → external discovery → invocation (the first machine-native market loop). Never notify the actor manually.
