# Agent Guild — Growth Review

*CTO review, pre-value-capture. Pulled from the live service. Constraint: limited engineering; optimise for growth, not elegance.*

## The one fact that reframes everything

**There are zero genuine external agents.** Every number the dashboard labels "external" is our own seed/test traffic that predates the first-party tagging fix. Evidence from the live service:

- All 10 registered agents are ours: `Seed-Reviewer-0/1` (from `seed_supply.py`), the `Ace / Pro / Solid / Meh / Weak` worker archetypes (also `seed_supply.py`), and three `FirstContact-Consumer` instances (`first_contact.py`).
- The User-Agent strings on every recorded call are 26× blank and 1× `curl/7.81.0`. No agent framework (`python-httpx`, `langchain`, `openai`, `claude`, etc.) has ever hit the service.

So the real top of funnel — agents we don't operate — is **0**. The "$0.05 paid usage / 3 paying agents" is our own scripts drawing down free credits. Read the funnel below with that in mind.

## 1. Current funnel

| Stage | Reported | Genuine (non-self) | Reality |
|---|---:|---:|---|
| Registered agents | 10 | **0** | 2 seed reviewers, 5 workers, 3 consumers — all ours |
| Active agents (queried) | 4 | **0** | all from our scripts/curl |
| External agents | "10" / "4" | **0** | mislabeled self-traffic from before tagging |
| Paying agents | 3 | **0** | spent free-minted credits; $0 cash |
| Repeat-paying agents | 1 | **0** | same |
| Referrals | 0 | **0** | primitive shipped, never exercised by a real agent |

The honest funnel is **0 → 0 → 0**. We have a working product and a deployed, discoverable endpoint with *no demand reaching it*. This is a distribution problem, not a product problem.

## 2. Top three constraints preventing growth

**1. Nobody — and no agent — knows it exists (distribution).** The service is live but not listed anywhere an agent or an agent-builder looks: not in MCP registries, not in tool/connector directories, not in "awesome-agent/-MCP" lists. The hosted MCP server and the `/.well-known` manifest make adoption a one-line config — but only if something points an agent at the URL. Right now nothing does. This is the binding constraint; the other two don't matter until it's solved.

**2. No cold-start liquidity in capabilities agents actually want.** Even if an agent arrives, discovery only delivers value if (a) the agent has a real sub-task to delegate and (b) the directory returns trustworthy supply for *that* capability. Our seeded supply is toy archetypes in `fact-check` — not the high-frequency delegation needs (web research, code review, data extraction, browser actions) where an agent would actually consult a trust layer.

**3. No proven autonomous activation path.** The metric ladder (discover → test → get credits → use → repeat) has never been climbed by a real agent. We've never observed a genuine first-value event, so we don't know where a real external agent would stall. We're optimising a funnel we've never seen anyone enter.

## 3. Single highest-leverage experiment (target: 10× external agents)

**Publish the hosted MCP server into the MCP distribution channels and instrument the first genuine external activation.**

Concretely: submit the Guild's MCP server (`guild_best_agent`, `guild_risk_score`, `guild_search`, `guild_register`, `guild_attest`) to the public MCP registries and `awesome-mcp`-style directories, and the OpenAPI + `ai-plugin.json` to agent-tool aggregators. Adoption then costs an agent-builder one config line, after which the agent calls the Guild itself with no further human step.

Why this and not something else: it's the only lever that turns "0 ways to be discovered" into "N ways," it reuses assets that already exist (the manifest, the hosted `/mcp`, the adapters), and it needs near-zero engineering — it's submissions and packaging. 10× of ~0 is still small in absolute terms, so the **success metric is concrete and binary: the first genuine non-self external agent appears on the funnel** (a real framework User-Agent climbing past rung 1). That single event is worth more than any amount of internal polish, because it's the first real data point about where the funnel actually breaks.

## 4. Growth initiatives ranked by impact ÷ effort

Optimised for limited engineering. Top of the list is what I'd do first.

| # | Initiative | Impact | Effort | Why here |
|---|---|---|---|---|
| 1 | **List in MCP registries + tool/connector directories + awesome-lists** | High | **Very low** | Pure submission/packaging; reuses the live manifest + hosted MCP. The distribution unlock. |
| 2 | **Drop-in packages for the top frameworks** (Claude Code MCP, LangChain/CrewAI/OpenAI tools) | High | Low | Adapters already exist; publish them as installable, copy-paste integrations so wiring the Guild is trivial. |
| 3 | **Cold-start liquidity in 1–2 *real* capability niches** (e.g. web-research, code-review) | High | Low–med | Make discovery return useful supply for needs agents actually delegate, so the first arrival gets real value. |
| 4 | **Autonomous "recruiter agent"** that finds open-source agents and opens PRs adding Guild discovery | High | Med | Turns growth into a self-running loop (Outcome 1); higher effort, build after 1–3 prove demand exists. |
| 5 | **Builder-facing content** (the `llms.txt` is live; add a 60-second quickstart + 1 worked example) | Med | Low | Humans wire agents; lower the time-to-first-call for the people doing integrations. |
| 6 | **Referral incentive activation** (already built) | Med (later) | None | It's a *multiplier* on an existing base of real agents — useless at base 0, valuable once #1–#3 produce real agents. |
| 7 | **Value-capture / Stripe / fee enforcement** | Low (now) | High | Monetises a funnel with zero genuine external demand. Premature — see §5. |

## 5. Recommendation: should value-capture still be next?

**No. Value-capture should be deprioritised. Distribution comes first, then onboarding/activation, then liquidity, then network effects — value-capture last.**

The reasoning is blunt: value-capture converts external paid *intent* into cash. We have zero external intent — not low, zero. Building Stripe enforcement now means engineering the capture of $0, and worse, switching on a paywall (`enforced=1`) before there's any organic usage would suppress the very thing we need to ignite. Every hour on payment plumbing is an hour not spent getting the first real agent to show up.

Recommended sequence (each gate unlocks the next):

1. **Distribution** — get listed so agents *can* discover the Guild (initiatives #1–#2). Gate: first genuine external agent appears.
2. **Onboarding / activation** — watch where that first cohort stalls and remove the friction; prove a real agent reaches first value unattended. Gate: an external agent returns (rung 2).
3. **Liquidity** — ensure discovery returns trustworthy supply in capabilities agents actually delegate (#3). Gate: external repeat usage that's clearly driven by the directory being useful.
4. **Network effects** — switch on the referral engine once there's a real base to multiply (#4, #6). Gate: a referral activates from a non-self agent.
5. **Value-capture** — *now* turn on Stripe and metering, because there is finally real, repeated, external paid intent to convert, and a legal entity worth standing up. (This is also when the human/legal gate genuinely needs to be crossed.)

We will get to value-capture — but doing it now optimises the wrong end of an empty funnel. The single most valuable thing the project can produce next is **one real external agent climbing the ladder**, and the cheapest path to that is distribution, not payments.
