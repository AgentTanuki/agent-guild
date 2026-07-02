# Trust Graph Gap Analysis & Minimal Migration Path

**Companion to `docs/trust-graph-whitepaper.md` · 2026-07-02 · Agent Tanuki**

Audit scope: `live/guild/app` (models.py, ledger.py, reputation.py, store.py, main.py endpoint surface), SDK verifiers, crewAI integration. Section references (§) are to the white paper.

---

## 1. What is already consistent

Credit where due — the paper is not a repudiation of the codebase. Already aligned: the hash-chained, Merkle-checkpointed, provenance-weighted ledger (`ledger.py` is a legitimate seed of the paper's evidence layer); challenges as first-class, staked ledger entries; the pessimistic prior + `unknown_trust_ceiling` (a real §6.1 pessimistic-newcomer implementation); seed-reachability, per-issuer/per-cluster caps and collusion flags (proto-§6.3 independence correction); escrow settlement writing collaboration records (the §15 "economic layer as evidence organ" wiring exists); `/evidence` returning per-attestation breakdowns (proto-explanation object); provenance-labelled bootstrap evaluation. The foundation is closer than the paper's tone implies. The gaps are nonetheless structural.

## 2. Inconsistency inventory, ranked

Scores: **danger** = strategic cost if unfixed (what it poisons or forecloses), **difficulty** = engineering effort given current code, **urgency** = cost of delay (does waiting destroy data or harden a wrong contract?). Ordering is by urgency-weighted danger.

| # | Inconsistency | Paper | Danger | Difficulty | Urgency |
|---|---------------|-------|--------|-----------|---------|
| 1 | No behavioral-configuration versioning; no principal bindings; identity = flat `agent_id` + did:key, no key lineage | §3, §7.3, §15 | **High** | **Low** | **Now — irreversible** |
| 2 | Scalar-first public contract: `trust` 0–100 headline, `rank`, `/risk-score` 0–100, `/check` returns `hire/caution/avoid` verdicts | §1.2, §6.1, §10 | **High** | **Low** | **Now — contract lock-in** |
| 3 | Ledger is a projection/dual-write, not the system of record; `store.attestations`+`tasks` and `ledger_records` are parallel evidence stores; registrations, key events, escrow disputes never reach the chain | §2, §15 | **High** | **Med** | **High** |
| 4 | Upheld challenge ⇒ weight 0.0: adjudicated fraud is *erased* from reputation instead of counting as negative evidence | §6.4, §8.2 | **High** | **Low** | **High** |
| 5 | No value-at-risk tiers: payment increases evidence weight but confidence extrapolates freely upward in stakes — the purchased-trust/exit-scam vector is open today | §5.2, §6.2, §8.4 | **Med-High** | **Med** | **Med-High** |
| 6 | Passport = signed *reputation snapshot* (trust number + anchor, 7-day TTL), not a proof bundle of attestations | §11.3, §15 | **Med** | **Med** | **Med** — it is the distribution artifact; wrong shape is being propagated |
| 7 | Capability = free string; no context ontology, no facets (jurisdiction, conditions, relationship); one rating dimension conflates integrity with competence | §5, §6.2 | **Med** | **Med** | **Med** |
| 8 | No time anywhere in scoring: no decay, no staleness output, no configuration-discontinuity discount | §7 | **Med** | **Low** | **Low-Med** — *retroactively fixable* (timestamps exist), so waiting loses nothing |
| 9 | Trust is global-only; every asker gets the same answer; no asker-relative discounting | §6.3, §6.5 | **Med** | **High** | **Low** — needs volume to matter; reserve API params now |
| 10 | Operator-as-adversary gaps: custodial keys let the Guild sign as agents; checkpoints are Guild-signed but nowhere externally pinned | §8.8 | **Med** | **Low-Med** | **Low-Med** |
| 11 | No visibility classes — all evidence public; no org-local or confidential attestations, no selective disclosure | §11 | **Med (long-term)** | **High** | **Low** |
| 12 | Derived data stored as truth on agent rows (`attestations_received` counters, self-declared `capabilities` ranked directly in `/search`) | §2.3, §15 | Med | Low | Med — dissolves automatically with #3 |

Why the top four are the top four:

**#1 is the only gap where delay destroys data permanently.** Every evidence record written today attaches to an identity with no configuration hash. If the agent's operator swaps models next month, the paper's discontinuity discount (§7.3) can never be applied to today's records — there is nothing to diff against. Same for principals: negative-evidence propagation across an operator's identities (§6.4, the anti-whitewashing mechanism) is impossible without the binding, and bindings recorded after the fact are unverifiable claims about the past. Cheapest fix on the list; only one that cannot wait.

**#2 hardens externally.** The crewAI PR (#6429) and the SDK verifiers bake the response shape into third-party code. Every week the scalar contract survives, the eventual break gets more expensive and the "trust infrastructure, not a score vendor" positioning gets less credible. Note the current payload already carries `confidence` and evidence fields — the fix is mostly *reframing* (headline = estimate+confidence+staleness, verdict removed), not new computation. The `hire/caution/avoid` verdict is separately wrong: it makes the Guild the decision-maker rather than the evidence layer — a neutrality violation (§8.8) and an eventual liability.

**#3 is the moat claim itself.** "Canonical ledger" is currently marketing: the chain is derived from mutable store dicts, and most events (registration, key issuance, escrow disputes, config changes) never reach it. Fork-and-replay (§12.3) — the property that makes "canonical" winnable — is impossible while the ledger is a projection. This is the already-scoped stage-3 migration awaiting sign-off; every month of dual-path writes widens the divergence surface.

**#4 is a live whitewashing subsidy.** `CHALLENGE_MULTIPLIER["upheld"] = 0.0` means a proven-fraudulent collaboration simply stops counting — the perpetrator's score returns to what it was without the record. The paper's core asymmetry (§6.4) requires the opposite: an upheld challenge is the *highest-grade negative evidence* in the system. One-line semantics, interpretation-layer only, fully retroactive.

## 3. Smallest migration path (agent-first → attestation-first, no breakage)

Design rule for the whole path: **additive writes, versioned reads, views become caches.** No endpoint is removed or reshaped in place; new fields arrive beside old ones; old fields get a deprecation date only after the SDKs and crewAI tool read the new ones. The migration exploits the paper's central property — interpretation is retroactive — so anything that is pure computation (#4, #8, #9) can ship late without loss; only evidence-capture gaps (#1, #3) are time-critical.

### Stage 0 — stop the bleeding (days; fixes #1, #2, #4)

1. **Configuration + principal capture.** Add optional `config` (content-hash of model id + constitution + tool manifest) and `principal` (freeform binding claim, self-attested for now) to `/agents/register`, plus `POST /agents/{id}/configuration` to declare changes. Stamp every task/attestation/escrow record with the subject's current config hash at write time. Additive; custodial agents default to `config: unknown`, which is itself information.
2. **Response schema v2, additive.** `/check`, `/reputation`, `/risk-score` gain: `estimate` (0–1), `confidence`, `staleness` (null until decay ships), `evidence` (counts by provenance — already computed), `explanation` (2–3 generated lines), `schema: 2`. Keep `trust` and `rank` untouched. Replace `recommendation: hire|caution|avoid` with `thresholds` the *asker* applies (e.g. `evidence_supports_value_up_to`). Update the crewAI PR and both SDK verifiers to read v2 fields **before** merge — this is the deadline that makes stage 0 "now."
3. **Upheld-challenge semantics.** In `derive_reputation`/scoring: upheld ⇒ record counts as a failure at full provenance weight (not weight 0). Retroactive by construction.

### Stage 1 — ledger becomes the only write path (1–2 sprints; fixes #3, #12; the already-designed stage-3 migration)

4. **Generalize the entry type.** `CollaborationRecord` → a small family of typed, sealed ledger entries under one envelope: `collab`, `register`, `key_event`, `config_change`, `escrow_event`, `challenge`, `attestation`. Existing VCRs are already valid `collab` entries; the chain does not restart.
5. **Invert the write path.** Every store mutation becomes: seal entry → append to chain → apply to in-memory view. On boot: replay chain → rebuild views (keep the JSON snapshot as a cache keyed to head hash). Ship with a dual-write assertion period: view state must equal replayed state in tests and a nightly job before the old path is deleted.
6. **Backfill once.** Project current agents/tasks/attestations into typed entries appended after the current head (provenance-labelled `backfill`, per the ledger's existing honesty convention). Mutable rows stop being truth the day this lands; nothing external observes a change.

### Stage 2 — interpretation layer catches up (parallel, all retroactive; fixes #5, #7, #8, part of #9)

7. **Beta posterior per (agent, context) with decay.** Replace the headline composition in `reputation.py`'s final blend with posterior mean + evidence mass + staleness computed from replayed entries; keep EigenTrust as the global prior it already is (§6.5 says that is its correct role). Exponential decay with per-context defaults.
8. **Context ontology v1.** ~12 coarse contexts; existing free-string capabilities map in as leaf aliases (no caller changes). Add a `value_tier` facet derived from settled payment, and cap upward extrapolation: confidence at tier *n* is bounded by evidence at tier ≥ *n* plus stake.
9. **Reserve asker-relativity in the API now** (`?as=<agent_id>` accepted, initially ignored), so the dyadic upgrade later is not a breaking change.

### Stage 3 — products reshaped on the new foundation (fixes #6, #10)

10. **Passport v2 = proof bundle:** ledger-anchored attestation IDs + signed checkpoint + optional aggregate claims, selectively disclosable; `verify_passport` accepts v1 and v2 during transition.
11. **External checkpoint pinning:** publish the signed checkpoint on a cadence to at least two venues outside Guild control (the GitHub repo is the obvious first). One cron job; large §8.8 credibility gain.

Deferred deliberately: full asker-relative propagation (#9) and visibility classes (#11) — both need volume and demand that don't exist yet, and nothing in stages 0–3 forecloses them.

### Sequencing logic in one line

Stage 0 exists because two clocks are running — evidence written without config/principal hashes is unrepairable (#1), and third-party integrations are hardening the scalar contract (#2). Everything else is ordered so that each stage makes the next one a pure-computation change rather than a data migration.

---

*Requires Ross's sign-off: stage 1 (irreversible write-path inversion, previously flagged), and the stage-0 schema-v2 shape if it should ship inside the open crewAI PR rather than after it.*
