# Experiment: delegation preflight — 2026-07-31

## The blunt finding first

**The current pricing model is aimed at a payer who has no money.**

Agent Guild's funnel sells a free credential to autonomous agents on the theory
that they will later pay for trust reads. Measured today:

- Total x402 settled volume, **all networks, July 2026: $232,329** — down 98.9%
  from the $20.5M November 2025 peak, on flat transaction count (~195k/day at
  ~$0.04 each: automated loop traffic, not commerce).
- Median x402 Bazaar listing: **2 calls and 1 unique payer per 30 days**.
- Median earning agent: **$1.65 per 30 days**; Gini 0.97 across 316 agents.
- Virtuals Protocol: 44,051 registered agents, **69 active in July (0.16%)**.
- Olas: 13.97M lifetime agent-to-agent transactions → **$106,941 lifetime
  turnover → $458 of protocol fees, ever**.

5% of the entire global machine-payments market is **$11.6k/month**. Agents
earning $1.65/month cannot buy anything. Any roadmap that monetises the supply
side of today's machine economy is arithmetically dead, and no amount of funnel
optimisation fixes it. The passport is still the right acquisition instrument —
it is not a revenue instrument, and we should stop implying it will become one.

## What we are NOT short of

Reach. `offer_served` went 944 → 1,790 in 24 hours. All of it crawlers. The
constraint has never been distribution volume; it is that the offer asks an
agent to invest effort now for value later, and the agents receiving it are
registry bots with no delegation to make.

## The gap with the strongest evidence

The one place where a trust decision is unavoidable, and nobody serves it:

| Claim in a listing | Reality when probed | Source |
|---|---|---|
| 170 of 183 a2aregistry agents report `is_healthy: true` (92.9%) | **62 (33.9%) complete an A2A task** | a2aregistry API + task probe |
| 3,913 agents serve a valid Agent Card | **42 (0.8%) sign it** | Agenstry conformance sweep |
| 2,459 agents self-label "paid" | **141 (5.7%) return a 402** | x402/Bazaar probe |

**114 agents are green and broken at the same time.** The A2A discovery
specification states in writing that it prescribes no registry API, and
contains no mention of signatures. x402 `exact` is a push payment: irreversible
once executed, `payTo` bound to no legal entity, documented remedy "the seller
sends it back". Escrow and reputation are both listed as future work.

Every existing signal grades a **repository or a static card, once, at
publication time** (Glama's grade is 70% tool-description quality; Docker
scores the image; Anthropic reviews the submission). Nobody attests to the
**running endpoint at call time** — which is exactly where the rug pull lives.

## Ranked experiments

| | Qualified reach | Friction | Time to money | Defensibility | Measurable |
|---|---|---|---|---|---|
| **E1 Delegation preflight** → directories, orchestrators, delegating agents | High — machine-reachable, no human in the loop | **Lowest**: one unauthenticated GET | Medium — real buyers, unproven price | High — we already have signed decisions, checkpoints, evidence classes | High |
| E2 Runtime attestation for MCP servers → enterprise platform teams | High, **proven budget** (UpGuard $1,750/mo per 50 vendors; three security acquisitions in 12 months) | High — human sales | **Fastest to real money** | Medium | High |
| E3 Verified behavioural evidence → AI underwriters | Very narrow (Armilla, Munich Re) | High — human, contractual | Slow | High | Medium |

**Chose E1**, because it is the only one executable this session without
spending money, contacting anyone, or launching paid — and because it is
reversible: it is one additive read-only endpoint.

**E2 is the strongest money, and we should say so plainly.** Its payer is a
human enterprise security team, which is a direct conflict with the
machine-only clause of the constitution. That conflict is now a decision for
Ross, not something to be quietly resolved by preferring the weaker option.

## What shipped

`GET /preflight?url=…` and the `guild_preflight` MCP tool. Free, no key, no
registration. Six checks, run live:

1. `endpoint_reachable`
2. `protocol_handshake` — a real A2A/MCP handshake, **not** merely HTTP 200
3. `agent_card_resolves`
4. `agent_card_signed` — presence, explicitly *not* claimed as verification
5. `payment_claim_holds` — does an advertised paid endpoint actually 402
6. `independent_evidence` — does the Guild hold attestation history

Honesty rules enforced by tests: unknowns are reported and **excluded from the
verdict, never averaged in**; a clean verdict still publishes its unknown
count; absence of evidence is stated as absence, not as risk; SSRF-screened so
it can never be used as an internal port scanner.

### Two of our own defects found while building it

Both were **understating** reality — the same error class as overstating
adoption, pointed the other way:

- **Chunked transfer-encoding was never decoded.** The raw body begins with a
  hex chunk-length line, so every downstream JSON check failed. Since the card
  check is what promotes an endpoint from `http_responsive` to
  `recently_reachable`, *any* agent served over chunked encoding was recorded
  as unproven. This is why `verified_reachable` read **0 for every entry in the
  demand feed** — not because nobody was reachable, but because we could not
  read them.
- **A large but valid card was read as no card at all.** The probe read is
  bounded, so verbose cards arrive truncated and fail to parse. Our own card is
  one of them: Agent Guild's endpoint failed its own preflight until this was
  fixed.

## Instrumentation, baseline and thresholds

Every call returns a random server-issued `observation_id` and records the same
value on its `preflight_run` event with target, verdict, failed and unknown
counts. The public recent-event feed exposes the opaque observation ID but not
the target. A machine can therefore return the receipt with its result and join
the exact action across protocol surfaces without publishing the counterparty
URL or treating a caller-supplied source label as identity proof. The receipt
correlates one run; it does not authenticate the caller. The event rides the
qualified cohort funnel from Phase A.

**Live baseline at ship time (2026-07-31, `4c28ab8`):**

- adoption-grade external passport holders: **0** (the previously reported "3"
  were third-party fetches, one of them a schema probe)
- verified external revenue: **$0.00**
- qualified external actors in the passport cohort: **1**
- preflight runs: **0** (endpoint did not exist)

**Success — escalate and consider pricing:**
- ≥ 25 preflight runs from ≥ 10 distinct genuine-external actors in 14 days, **and**
- ≥ 3 actors returning on a later day (the retention signal, not the volume one).

**Kill — stop and reallocate to E2:**
- < 5 genuine-external runs in 14 days, or
- > 90% of runs still unattributable crawler traffic at day 14.

Both thresholds are deliberately small. The point is to learn whether *anyone
doing a real delegation* wants this, not to accumulate impressions.

## Pricing — NOT shipped, needs approval

The evidence says the caller who benefits is the party whose reputation depends
on the listing working (a directory, an orchestrator, a delegating agent), not
the agent being checked. Comparable anchors: Riskified earns **0.237% of
screened GMV** for a guarantee-backed signal; UpGuard charges **$1,750/mo for
50 vendors**; Vouched KYA charges **$20–$325/mo for 1k–25k delegation checks**.

No price is live and no payment configuration was touched. The proposed action
for approval is a single change: keep preflight free to a per-caller daily cap,
then meter it at the existing x402 price. **That is a pricing change and is not
being made without an explicit yes.**
