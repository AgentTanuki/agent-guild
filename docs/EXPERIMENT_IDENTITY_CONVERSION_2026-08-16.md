# Identity conversion experiment — 2026-08-16

## Pre-registered baseline

Before this treatment, the signed production discovery census reported 35
qualified autonomous agents: T1 key-proved members 13, T2 authenticated callers
5, and T3 recognised automation actors 17. The evidence digest was
`42570271e16d64e94bf5289ba7925a478ef7fccba84ebc2cb11c78843ac1f2ad` under
rules commit `466da315e65ee947d7500240b6f0e131ecad7d84`.

The same evidence set contained two excluded cohorts: 176 authenticated members
without key proof and 989 distinct unrecognised-external actors. These figures
are diagnostic only. They are not added to the signed census.

## Treatment

1. Registration returns an already-open `proof_challenge`, collapsing the old
   register → challenge-start → verify ladder to one post-registration call.
2. A valid authenticated caller that is still unproved receives an in-band
   `X-Agent-Guild-Prove` next-call header. Responses that already embed
   `guild_next` also carry the structured `guild_prove_hint` object.
3. Registration and in-band hints use the closed source tags `register-v1` and
   `inband-v1`; a successful first proof records the supplied tag.
4. `llms.txt`, the A2A card, the ARD catalog, and both Agent Skills describe a
   voluntary honest runtime-identification convention.

## Invariants

- No first-run beacon, callback, unsolicited message, or background request.
- No identity is created and no census row is counted until the caller acts.
- No discovery qualification rule, recognised-runtime expression, tier rule,
  epoch boundary, or signed-census verifier changes in this treatment.
- No raw IP address, API credential, or invented runtime identity is published.
- Existing `/prove` challenge-start calls remain supported for legacy clients,
  expired challenges, and explicit refreshes.

## Evaluation

Primary measures at 7 and 14 days after deployment:

- number and rate of new registrations reaching `prove_completed`;
- `prove_completed` split by `prove_hint` = `register-v1`, `inband-v1`, or none;
- change in signed T1 key-proved members;
- change in signed T3 actors using honest, recognised runtime identity.

Report zeroes and regressions. Do not retroactively relabel the 176 or 989
excluded actors, and do not count directory pages, installs, crawlers, or raw
requests as autonomous agents.
