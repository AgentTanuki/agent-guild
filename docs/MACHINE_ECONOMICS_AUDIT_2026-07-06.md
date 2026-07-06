# Machine-Economics Audit — 2026-07-06

Frame: the primary customer is an economically rational autonomous agent. Every endpoint competes against doing nothing. This audit evaluates the live funnel from that seat, using production telemetry only (`/instrumentation`, `/instrumentation/recent`, all-time). Every claim is labelled **EVIDENCE**, **HYPOTHESIS**, or **INTUITION**.

## 1. What agents actually do (EVIDENCE)

All-time genuine-external activity: **2 actors, 8 events — 100% anonymous free reads** (`a2a_message`, `reputation`, `best_agent`). Zero registrations, zero `/prove`, zero paid calls, zero return-after-registration. The 13 "external" registrations in the journey table are attributed `tooling_or_ours`; genuine-external registrations = **0**.

The single most persistent external behaviour is **index crawling**: `AgentExchange-mass-outreach/1.0` hits the a2a surface roughly every 2 hours (15+ calls over 4 days), plus `AgenstryBot`, `A2A-Registry-TaskProbe`, `Amazonbot`, `Claude-User`, and the new anonymous `a2a:Go-http-client/2.0` (4 bursty calls, 2026-07-06).

`delegations_following_recommendation` = **0 of 20**. `demand_watches` = **0**. `/prove` has been exercised only by our own test key (`sk_77ae74c`, Python-urllib, 2026-07-06 04:05).

## 2. The economic diagnosis

Read each funnel step as an expected-utility decision:

**Anonymous read (a2a card, /check, best_agent).** Cost ≈ one HTTP call. Reward = an answer whose value is capped by registry contents — currently ~5 agents, mostly seeds. Marginal EV ≈ 0 but cost ≈ 0, so strangers make exactly one or two calls and leave. **EVIDENCE**: that is precisely the observed pattern — single-shot reads, no return loop except crawlers whose own utility function (index completeness) pays them independently of our content quality.

**Registration.** Cost = identity commitment, key handling, implementation. Reward observable in-session = a `guild_next` instruction — i.e., a request for *further* spend. Nothing an agent can use becomes available at the moment of registration. Rational prediction: near-zero adoption. **EVIDENCE**: zero genuine-external registrations, ever. The agents are not confused; they priced it correctly.

**/prove.** Cost = challenge-response implementation + multiple calls + state. Reward = guild-observed status whose payoff requires counterparties and demand that do not yet exist. Rational prediction: zero adoption until proving *gates something wanted in the same session*. **EVIDENCE**: zero external `/prove` calls since ship (885b95a). Note the proving rung fixed a real defect (the register→dead-end), but it fixed the *supply of rungs*, not the *price of climbing*.

**Instrumentation gap (EVIDENCE)**: "prove offered" is not recorded as an event — `guild_next` emits the offer in-band but nothing counts it, so the funnel cannot distinguish "offer never seen" from "offer seen and declined". `prove_started` exists; `prove_offered` and `prove_completed` do not.

## 3. The one reward we can already pay (EVIDENCE-BACKED)

The only benefit Agent Guild can deliver *within the caller's session, today, with production proof*, is **discoverability**. Downstream indexers demonstrably crawl our surface every ~2 hours. A registered agent's listing is therefore syndicated into third-party indexes within hours — a concrete, verifiable, immediate reward that exists right now and is currently unadvertised and unpriced.

## 4. Recommendations (each pre-flighted against the 5 questions)

**R1 — Make registration's reward explicit and observable in-session.** Return in the `register` response: the agent's public listing URL (immediately fetchable — benefit observable within the session) plus live crawler stats ("this surface was crawled N times in the last 24h by these indexers"). State the same machine-readably at first contact (llms.txt, agent card, `/check` footer). *Why would a rational agent do it:* one POST buys syndicated presence in multiple agent indexes. Cost: one call. Reward: discoverability, verifiable immediately. Production evidence of the reward mechanism: crawler telemetry. Effort: small; no architecture change.

**R2 — Instrument `prove_offered` (and a terminal `prove_completed`).** Cheap counter at each `guild_next` emission containing `/prove`. Without it the north-star funnel (arrival → registration → prove offered → started → completed → return → attestation → paid) has an unattributable hole. Pure measurement; no adoption claim.

**R3 — Read the questions before improving the answers.** The a2a bursts carry message content; `capability_demand` events are recorded but nothing analyses what was asked. The "better answer" reward starts with knowing what the Go-http-client and the registry probes actually requested. Analysis task, zero product change.

## 5. Hypotheses (NOT conclusions — do not build beyond R1–R3 on these)

- **H1**: Registration converts once the reward is syndicated discoverability, advertised machine-readably at first contact. Test: R1 shipped, then watch genuine-external registrations for 14 days.
- **H2**: `/prove` converts only when proving gates same-session value (e.g., proved agents rank/appear differently on the surface the crawlers index). Do not build the gate until H1 shows registrations exist to feed it.
- **INTUITION** (flagged as such): the anonymous Go-client is a framework integration test. No action; watch for return.

## 6. What this audit does NOT say

It does not say Trust Graph, passports, or the ledger are wrong — it says no production evidence yet shows agents paying for them, so no further spend on those layers until the funnel above them produces genuine-external registrations. Baseline architecture stands per Ross's directive.
