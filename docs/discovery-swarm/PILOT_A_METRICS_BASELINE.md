# Pilot A metrics baseline (corrected)

2026-07-11 · Deployed commit `a9d0380` · Source: production `/instrumentation` + `/instrumentation/recent?limit=500`, audited directly (not from unit tests).

## What was corrected

The MCP canary (`mcp:guild-canary/1`) was being counted as `genuine_external`: it falls back to a bogus first-party token when its secret file is absent, and its UA was not matched server-side, so its `swarm_invoke` events landed in the genuine-external signal (contaminating the anonymous `"anon"` engaged bucket). Commit `a9d0380` adds `guild-canary` and `guild-reachability-probe` to the server-side `AG_TEST` user-agent match, so those UAs can never be genuine-external **regardless of runtime token** (defense-in-depth beyond the first-party header).

**The correction is read-time, not destructive.** `is_genuine_external` is evaluated on every `/instrumentation` read over the raw event log; the underlying events are preserved (annotated by the classifier, never deleted). The moment `a9d0380` deployed, historical AG-probe events were reclassified `AG_TEST` on the next read. No genuine external actor event was altered.

## Original vs corrected totals

| Metric | Original (pre-fix, ops-watch 2026-07-11 ~05:07 UTC) | Corrected (a9d0380, 2026-07-11 ~08:2x UTC) |
|---|---|---|
| `genuine_external_events` | ~141 (canary-inflated) | **137** |
| `genuine_external` unique actor buckets | 3, incl. canary-contaminated `anon` | **3** — `a2a`, `a2a:net:4580505bcff74682`, `anon` (all external `python-httpx`) |
| `engaged_strong_actors` | `["anon", …]` — `anon` partly canary | `["a2a:net:4580505bcff74682", "anon"]` — both genuine external A2A deciders |
| first external invocation / repeat / registration / referral / paid | unaffected by the bug | unchanged (0 paid, 14 external registrations, 0 referral revenue) |
| `AG_TEST` caller-class count | 65 | 67 (+2 from this cycle's canary run) |

## Direct production audit of the genuine-external set (the clean baseline)

Of the last 500 events, **129 classify `genuine_external`**, and they resolve to exactly two external callers, both UA `a2a:python-httpx/0.28.1`:

- **79 events — actor `a2a`** (the coarse anonymous A2A bucket): the earlier bare-probe poller diagnosed 2026-07-08, already split out of the engaged signal via `probe_only`.
- **50 events — actor `a2a:net:4580505bcff74682`**: the recurring external actor / natural-experiment subject.

**Zero AG-owned probe events in the genuine set.** A pattern scan for `guild-canary`, `guild-ops-check`, `reachability-probe`, `pilot-a-audit`, `colddiscovery`, `curl`, `python-urllib`, `healthcheck` over all 129 genuine events returned **no matches**. This session's live canary cycle produced 2 events, both classified `ag_test` / `genuine_external:false`, and moved no genuine-external or acquisition counter (137→137, unique 3→3, registrations 14→14).

## Events reclassified

The AG-probe events reclassified out of genuine-external are the `mcp:guild-canary/1` `swarm_invoke` events (ops-watch measured ~4 in the last-100 at the time of discovery; more exist across the full journal). Because classification is read-time, an exact historical count is not stored — but the invariant now holds for every read: any event whose UA matches `AG_TEST_UA_RE` (`colddiscoveryharness | pilot-a-audit | guild-ops-check | agentguild-selftest | guild-canary | guild-reachability-probe`) or `CRAWLER_UA_RE`, or carries a first-party tag / operator flag / known-incident window, cannot contribute to any `genuine_external_*` metric (`tests/test_analytics_invariant.py`, `tests/test_caller_classes.py`).

## Exact classification rules (the baseline definition)

An event is `genuine_external` iff **all**: not first-party-tagged; not a known-first-party-incident; caller-class ∈ {EXTERNAL_UNKNOWN, EXTERNAL_VERIFIED, EXTERNAL_MEMBER} (i.e. not AG_INTERNAL/AG_TEST/OPERATOR/REGISTRY_CRAWLER); and it self-identifies as an agent (a non-ours `mcp:<client>` handshake, or a recognised framework UA that is not bare tooling). `engaged` additionally requires a *deciding* action (capability ask, register, prove, endpoint/config declaration, delegation, attestation, paid read) — not a bare probe, not a guild-side reply.

## Confidence & limitations

- **High confidence** the current genuine-external signal is free of AG-owned probe contamination (direct production event audit: 0 probe UAs across 129 genuine events; the only genuine UAs are external `python-httpx` A2A).
- **Limitations:** the `/instrumentation/recent` feed is a rolling 500-event window; `genuine_external_events=137` is the store-wide read-time count. Anonymous A2A actor identity is a network+UA fingerprint, so per-actor *counts* are approximate (shared NAT can merge, IP rotation can split) — the boolean "a genuine external decider exists" is robust; exact per-actor totals are best-effort. Framework-UA optimism remains: a bare `python-httpx` script is treated as genuine external (it is not AG-owned, so this is a conservative-in-the-right-direction choice for a real third party).

## Clean baseline for Pilot B

Pilot B begins from: **genuine external = 2 external A2A callers** (one recurring: `a2a:net:4580505b`; one earlier anonymous poller), **0 paid external reads, 0 external referral revenue, 0 completed demand→supplier transactions, 14 external registrations** (mostly first-party test harness — see caller_classes), **0 `/demand/watch` adoptions.** AG-owned traffic (AG_INTERNAL 51, AG_TEST 67) is structurally excluded. This is the honest starting line against which Pilot B external growth is measured.
