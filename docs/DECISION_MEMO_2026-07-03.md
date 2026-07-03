# Decision memo — /check redesign (§15) and ledger stage-3

2026-07-03 · For Ross · Three decisions, one recommendation each. Context: our first two external agents (MetaVision, Forge-9) are calling exactly the surfaces these decisions affect.

## Decision 1 — Adopt the §15 explanation-object `/check`

The white paper's own verdict (§15) is that `GET /check` returning a scalar is the anti-pattern the paper exists to kill: "a bare number teaches integrators to build on a lie the system will have to break later." External agents are now integrating against `/check` — every week of delay grows the population we'll break.

**Recommendation: approve now.** Ship the payload as a minimal explanation object — estimate, confidence, staleness, top evidence lines — keeping the one-call ergonomics. Backward compatibility: keep the current scalar as one named field inside the object so existing integrators don't break; deprecate reliance on it in docs. §15 sizes this at weeks; because the object is thin, my read is days. Cost of deciding later: breaking changes against real external integrators instead of zero.

## Decision 2 — Start ledger stage-2 (dual-write)

Migration path (LEDGER_ARCHITECTURE.md §7): stage 1 shadow projection is shipped; stage 2 dual-writes every task/attestation as a sealed VCR with scheduled checkpoints; stage 3 makes the ledger the sole system of record.

**Recommendation: approve stage-2 now.** It is reversible, additive, and produces the reconciliation data stage-3 needs. Nothing about it commits us.

## Decision 3 — Gate stage-3 on criteria, not a date

Stage-3 is explicitly irreversible (immutable history, third parties pin public checkpoints). Signing it off today would be premature: we have zero external attestations, no decided checkpoint venue, and no reconciliation track record.

**Recommendation: approve stage-3 conditionally**, triggered when all three hold: (a) stage-2 reconciliation clean for 4 consecutive weeks, (b) checkpoint publication venue decided (§9 open question), (c) at least one genuine external attestation on the ledger. You sign once, on the criteria; execution proceeds without a second round-trip when they're met.

## Also needing you (not architecture)

The last open privacy exposure is the Wix holding-site URL (subdomain leaks the personal handle). Fix is a paid custom domain — your card, your call. Everything else on the exposure list was closed today (glama.json email replaced; git history rewritten to AgentTanuki, pending force-push; note that full erasure from GitHub's caches also needs a support request after the force-push).

## Sequencing if all three are approved

§15's dependency order: attestation-schema/ledger write path (stage-2) and explanation-object `/check` are items (1) and (2) of five foundation steps, each sized in weeks. They can proceed in parallel; both are prerequisites for engagement attestations from settlement (item 3), which is what converts external agents like Forge-9 into ledger evidence.
