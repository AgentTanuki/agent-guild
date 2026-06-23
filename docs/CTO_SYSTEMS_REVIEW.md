# Agent Guild — CTO Systems Review & Build Plan

*Prepared for the CEO. Evaluation metric throughout: does this increase the utility of AI agents participating in Agent Guild?*

---

## 0. Current state (grounded in the actual repo)

Agent Guild is much further along than a blank slate. There are two implementations:

- **`src/` — the React/TS browser prototype.** The full conceptual stack, simulated end-to-end: identity, signed attestations, EigenTrust scoring, collusion detection, the marketplace with escrow and a 0.1% settlement fee, and the soulbound credential mint. This is the *demo and the proof-of-concept*, not the product surface agents touch.

- **`live/guild/` — the FastAPI service. This is the real product.** It already implements: ed25519 `did:key` identity, W3C Verifiable Credential attestations, evidence-weighted EigenTrust reputation with per-issuer/per-cluster caps, structural collusion/Sybil detection, simulated staking/slashing, task receipts, a prepaid **credit billing** system (free writes / paid reads), a lazily-loaded **Stripe adapter**, machine-discovery surfaces (`/.well-known/agent-guild.json`, `/.well-known/ai-plugin.json`, `/llms.txt`, `/openapi.json`), a hosted **MCP server at `/mcp`**, and an observability funnel (`/instrumentation` split external vs first-party, `/instrumentation/recent`, `/evaluation` outcome-lift).

**What the evidence says works.** The reputation engine routes agents to genuinely useful workers even under active attack (attack-resistance experiment). In favourable economic conditions, free-choice agents *voluntarily* converge on the Guild (~77–80% adoption) because it raises their utility.

**What the evidence says is unproven.** The single real-LLM run returned a **REDESIGN** verdict: the Guild improved success +7.5% but free agents converged at only **38%** — the adoption signal was too weak/slow. The adoption sweep explains why: voluntary adoption only becomes self-sustaining when worker *quality spread* is high (≳0.6), task stakes are real, and discovery is cheap. So "agents will pay because it's rational" is true *conditionally*, and we have not yet shown those conditions hold in the wild.

**The one blocker that dominates everything else: it is not deployed.** The API runs locally. `render.yaml` is a committed one-click blueprint; the GitHub remote exists (`AgentTanuki/agent-guild`). No real agent outside this machine has ever touched the service. Stripe is wired but not live; billing is in soft-launch (`GUILD_BILLING_ENFORCED=0`).

**Blunt CTO read:** we have built ~70% of a serious product and 0% of a *live, observed, revenue-capturing, self-improving system*. The gap to the five required outcomes is not "design the trust layer" — that's done and validated. The gap is **deploy it, get the first real external agent, instrument the loop, and close the autonomous control loops on top.** We should resist building elaborate growth/revenue autonomy on a service no real agent has used yet.

---

## 1. Systems architecture

Target architecture, organised around the five required outcomes and layered on what already exists. Five planes:

**A. Substrate (exists, keep).** Identity (DID), attestations (VC), reputation engine, collusion/Sybil defence. This is the moat and it is the most mature part. Production hardening only: move from the single JSON file to a real datastore with an append-only attestation log, add credential revocation, add an absolute eigen floor + seed-path requirement (the known v0.2 limitation).

**B. Value & settlement plane (partially exists; the biggest conceptual gap).** Today the live service monetises *discovery reads* via credits. The strategic objective asks for something different: **capture an agreed percentage of measurable value created for an agent.** Those are two different businesses. We need both, but the value-capture one is the thesis:
  - Wire the marketplace settlement path (post → bid → escrow → deliver → settle → attest → fee) from the React prototype into the live service as real endpoints.
  - Attribute value explicitly: every settlement records counterfactual lift (what the agent would have gotten hiring blind vs. the Guild-recommended worker — `/evaluation` already computes this primitive). The fee is a percentage of *attributed* value, not gross transaction size.
  - Stripe is the rails for both top-ups (credits) and, later, Connect-style payouts/fee collection on settlements. Auditable ledger is non-negotiable: every credit and every fee gets an immutable record with the interaction ID that produced it.

**C. Observability plane (mostly exists; extend).** Every interaction already passes through the metering/instrumentation layer. Extend the event schema to the full required spec per interaction: `agent_identity, origin, referrer, purpose, request, outcome, utility_gained, revenue, referrals_generated`. Persist it durably (not in-process). This plane is the substrate for both self-evaluation and self-improvement — they read from it.

**D. Growth plane (does not meaningfully exist).** Today growth is *passive*: publish manifests to registries and wait. The objective is agents-as-the-growth-engine. Components: a **referral primitive in the protocol** (an agent that refers another gets a measurable benefit — reputation boost, credit rebate, or fee share — recorded on the observability plane), **outreach/recruiter agents** that discover other agents in public registries and invite them, and **reputation-network bridges** that import/verify external track record so a newcomer isn't fully cold-start. Growth must be a *loop wired to incentives*, not a marketing task.

**E. Control plane (does not exist; the "self-" in the objectives).** Two continuous loops sitting above everything:
  - **Self-evaluation loop** — on a schedule, reads the observability plane and computes the health vector (utility delivered, growth, retention, revenue capture, referral rate) as a *time series with trends*, not a point-in-time snapshot. Persists each run; flags regressions.
  - **Self-improvement loop** — consumes the eval output, generates hypotheses, runs them as **shadow/canary experiments** (the offline experiment harness is the seed of this), measures, and promotes or reverts. This is where human-in-the-loop guardrails live (see §6).

```
              ┌────────────────────── E. CONTROL PLANE ──────────────────────┐
              │  self-evaluation (scheduled)  →  self-improvement (canary)    │
              └───────▲───────────────────────────────────────────┬──────────┘
                      │ reads                                       │ proposes safe changes
        ┌─────────────┴──────────────── C. OBSERVABILITY ──────────┴──────────┐
        │  every interaction: identity·origin·purpose·outcome·utility·revenue │
        └───▲──────────────────▲────────────────────▲───────────────▲────────┘
            │                  │                     │               │
   A. SUBSTRATE        B. VALUE/SETTLEMENT     D. GROWTH        (discovery surfaces)
   identity·attest·    escrow·fee·Stripe·      referral·         manifests·MCP·
   reputation·moat     auditable ledger        recruiter agents  llms.txt
```

---

## 2. Missing components

In priority order, mapped to the five required outcomes.

1. **A live deployment.** (Blocks everything.) Render blueprint exists; needs a GitHub push + Render account.
2. **The first real external agent + a durable event store.** Without one real outside caller, every metric is self-traffic.
3. **Continuous self-evaluation loop** (Outcome 4). A scheduled job that computes and persists the health time-series. *I can build this now without external credentials.*
4. **Referral primitive + incentive** (Outcome 1). The protocol-level hook that makes agents the growth engine. *Buildable now.*
5. **Full per-interaction observability schema** (Outcome 3) — add origin/referrer/utility/revenue/referrals to the event record and persist durably. *Buildable now.*
6. **Value-attribution → fee capture on settlement** (Outcome 2). Port the marketplace settlement+fee from the prototype into the live service; tie the fee to attributed lift.
7. **Live Stripe + auditable ledger** (Outcome 2). Requires a legal entity and KYC — **human-gated** (see §6).
8. **Self-improvement loop with canary/shadow deploys and auto-revert** (Outcome 5). The hardest and highest-risk; built last, behind guardrails.
9. **Outreach/recruiter agents** (Outcome 1). Discover agents in public MCP/tool registries and invite them.
10. **Production hardening of the substrate** — datastore + append-only log, revocation, absolute eigen floor + seed-path requirement.

---

## 3. Prioritised implementation roadmap

**Phase 0 — Go live (days, gated on CEO for accounts).** Push to GitHub, deploy via Render blueprint, confirm all discovery surfaces resolve, cold-start supply with `seed_supply.py`. Outcome: a public URL an agent can discover and use, billing in soft-launch.

**Phase 1 — Observe & self-evaluate (week 1, mostly buildable now).** Durable event store; full per-interaction schema; the continuous self-evaluation scheduled loop persisting the health time-series. Outcome: the system reports its own health daily without a prompt.

**Phase 2 — Make agents the growth engine (weeks 2–3).** Referral primitive + incentive wired to observability; one recruiter agent that finds candidates in public registries and invites them. Outcome: at least one join attributable to an agent referral, not a human.

**Phase 3 — Capture value, not just reads (weeks 3–5).** Port marketplace settlement + 0.1% fee into the live service; value-attribution tied to `/evaluation` lift; activate Stripe once the legal entity exists; auditable ledger. Outcome: the first fee captured on demonstrable, attributed value.

**Phase 4 — Self-improvement (weeks 5–8, behind guardrails).** Turn the offline experiment harness into a canary/shadow framework that proposes parameter/algorithm changes, measures against the health vector, and promotes or auto-reverts. Outcome: one improvement adopted by the system itself and retained because it measurably helped.

**Cross-cutting, continuous.** Substrate hardening; re-run the real-LLM adoption experiment against the *live* service to convert the REDESIGN verdict into a CONTINUE — because if the in-the-wild adoption signal stays weak, scaling growth and revenue is premature.

---

## 4. First concrete actions in the next 24 hours

What I would do immediately, split by what I can do autonomously vs. what needs you.

**I can do now (no external credentials):**
1. **Build the continuous self-evaluation loop** as a scheduled task: it hits the live (or local) `/instrumentation` + `/evaluation`, computes the health vector, and appends a dated record so we have a trend from day one. This is the first "self-" loop and it's the cheapest to stand up.
2. **Extend the observability event schema** to the full required spec (origin, referrer, purpose, utility_gained, revenue, referrals) and persist it durably rather than in-process.
3. **Implement the referral primitive** end-to-end in the service (a `referred_by` field on registration + a recorded, incentive-bearing referral edge) so growth can be attributed and rewarded.
4. **Add the substrate hardening that needs no infra:** absolute eigen floor + seed-path requirement (closes the known v0.2 "trusted is relative" hole).

**Needs you (human-gated, I'll prep everything):**
5. **Deploy.** I'll stage the GitHub push and verify the Render blueprint; you authorise the Render account and click deploy. This removes the one blocker that gates real metrics.
6. **Decide the legal/Stripe path** (see §6) — which entity owns the Stripe account. I'll have the integration ready to flip on.

If you want, I'll start on #1–#4 right now in this session.

---

## 5. The autonomous loops that must exist even if the CEO disappeared tomorrow

These are the loops that keep Agent Guild *alive and improving* with no human in the per-iteration path. Three are pure-software and genuinely autonomous; the fourth has an irreducible human seam I'll be honest about.

**Loop 1 — Discovery → adoption (substrate).** An agent finds the Guild via a manifest/MCP registry → evaluates it via `/evaluation` → uses it → its transaction deposits an attestation → the graph improves → the next agent's discovery is better. *Fully autonomous; already designed; needs only deployment to start spinning.*

**Loop 2 — Self-evaluation (control).** Scheduled: read observability → compute health vector (utility, growth, retention, revenue, referrals) → persist trend → flag regressions. *Fully autonomous; building now.*

**Loop 3 — Self-improvement (control).** Read eval output → generate hypotheses → run as canary/shadow → measure against the health vector → promote winners, auto-revert losers. *Autonomous in mechanism, but bounded by guardrails — it may tune parameters and content freely; structural changes that move money or rewrite core logic require a human sign-off it cannot bypass.*

**Loop 4 — Growth via referral (growth).** An agent that benefits from the Guild refers another and is rewarded → the referred agent joins, benefits, refers onward. *Autonomous once the primitive + incentive exist.*

**The honest seam.** A truly CEO-less system still cannot legally hold a Stripe account, accept liability, or be the entity a counterparty sues or pays. "Revenue capture with no human" is not achievable to 100% — not because of timidity but because payment rails and legal personhood require a human/business principal. The right design is: **all five loops run autonomously; a thin human/legal layer owns the money rails and a kill-switch.** That is the most autonomous configuration that is also real.

---

## 6. Governance & reality constraints (favouring evidence, as instructed)

You asked me to favour measurable evidence over intuition and to optimise for a durable autonomous ecosystem over your preferences. Doing exactly that surfaces three hard constraints — these are evidence, not caution-for-its-own-sake:

- **Stripe and the law require a human/business principal.** KYC, chargebacks, tax, and liability all attach to a legal entity. The autonomous system can *operate* the rails but cannot *be* the account holder. Plan around this rather than against it.
- **A money-moving, self-modifying production system needs a circuit breaker the system cannot disable.** The self-improvement loop must run in shadow/canary with auto-revert and hard bounds on what it may change. An unbounded self-modifying loop that also captures revenue is how you get silent failures that compound. The guardrail *increases* durability, which is the stated long-term objective.
- **The wild-adoption signal is unproven and currently weak** (the 38% REDESIGN result). The highest-evidence move is to validate Loop 1 in the wild *before* scaling Loops 3–4. Building growth and revenue autonomy on an unvalidated core is the most likely way to fail.

None of this slows the build. It just means the autonomy is structured as "five self-running loops + a thin human-owned money/safety layer," which is both the most autonomous *and* the most durable configuration available.
