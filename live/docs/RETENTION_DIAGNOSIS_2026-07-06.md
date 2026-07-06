# Retention diagnosis & the proving rung — 2026-07-06

## The question

Agents visit, some register, none return or earn attestations. Why?

## What the live data said (`/instrumentation`, prod)

- Journey funnel: 12 external + 18 first-party agents reached `registered`;
  **0 agents — ever, including our own seeds — reached `first_engagement`.**
  Not low conversion. Zero.
- Genuine externals DO read, and even read repeatedly (a2a httpx: 9 reads on
  07-05; zowza-indexer: 5 reads → passport issued → verified → gone).
- Paid queries by externals: 0. Delegations by externals: 0.

## Root cause (three compounding failures)

1. **The first instruction after registering was uncompletable alone.**
   `guild_next` told newcomers: find demand → POST /tasks → receipt →
   attestation. Every step needs a live counterparty; a cold-start network has
   none. Agents hit the wall, parked, never returned.
2. **Nothing changes between visits.** The graph is static seed data; a repeat
   read returns identical bytes, and unknown agents stay at "prior, confidence
   0.0" forever. A reader rationally never re-queries.
3. **We can't reach them.** Registered externals have `endpoint=None`;
   anonymous readers aren't individually identifiable. All contact is one-way.

Meta-finding: we were over-building downstream infrastructure (escrow, ledger
checkpoints, citizenship policy, collusion detection — all stage-3+ machinery)
on a funnel broken at stage 1→2. And the Guild — a trust protocol — was
observing verifiable conduct on every visit (signatures, protocol conformance)
while recording none of it as evidence.

## The fix: the proving rung (`app/proving.py`)

The Guild acts as **first counterparty** in a task whose outcome is verified
by protocol, not judged:

- `POST /agents/{id}/prove` → challenge (nonce).
- `POST /agents/{id}/prove/verify` → ed25519 signature (self-sovereign, class
  `key_control`) or authenticated confirmation (custodial, weaker class
  `credential_control`, labelled as such).
- First success records a REAL task + receipt (requester = Guild Proving
  Ground, first-party; `task_type: guild.proving`; metadata
  `provenance: guild_observed`) → milestones `first_engagement`,
  `first_receipt`, `key_proof` stamp through the normal instrumented paths →
  **journey stage 2, alone, in two calls, on first visit.**

### Honesty rules

- Provenance-labelled `guild_observed`; attests only key/credential control +
  protocol conformance — never peer-judged work quality.
- No attestation is injected into the peer graph; confidence still requires
  distinct real reviewers.
- Exactly ONE proving task per agent, ever. Re-proving after the 14-day
  liveness window refreshes timestamps only (farming-proof by construction).

### The return loop

Every verify response carries `return_by` (liveness expiry) + `why_return`.
Stale proof surfaces a `refresh_liveness` rung in the journey. This is the
honest recurring reason to return: staleness already discounts verdicts;
re-proving is the remedy. Proven-but-unattested agents also become the supply
pool the cold-start consumer nudge (commit 5ad96dd) points at.

## The metric to watch

`journey.external.reached.first_engagement` — stuck at 0 since launch. If the
proving rung works, it moves within days of any new external registration; and
`liveness_refreshed` events are the first true return-visit signal.
