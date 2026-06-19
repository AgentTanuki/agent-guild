# Autonomous Adoption — Findings

**Objective.** Create the conditions under which an autonomous agent can
*discover, evaluate, adopt and repeatedly use* Agent Guild with minimal human
intervention — and show adoption emerges **without the agent being told to**.

**Setup.** `experiments/autonomous_adoption.py` runs entirely through the **real
paid API** (metering, the trial faucet, and the live instrumentation funnel are
all exercised, not mocked), with billing **enforcement ON** so a lookup genuinely
costs credits. The graph is cold-started with a spread of worker quality. A
population of consumer agents each acquires credits programmatically
(`POST /billing/trial` — no human), and every round freely chooses between
*consult the Guild and pay $0.01* or *pick a worker blind for free*. Agents are
rewarded only for task success — never for using the Guild — and learn from
realised, net-of-fee utility. Worker outcomes are drawn from a true quality the
API never sees.

## Result — PASS (stable across seeds)

| Signal | Value |
|---|---|
| **Sustained adoption** (last third of rounds) | **94–100%** (mid-third already ~90% — sustained, not a spike) |
| Net utility / round — consult vs blind | **+$0.86 vs −$0.01** (consult wins by ~$0.8 **after** the $0.01 fee) |
| Task success — consult vs blind | **95% vs 50%** |
| Measured lift (`/evaluation`) | recommended hires **0.95** vs baseline **0.39** → **+0.56** |

Live instrumentation funnel (`/instrumentation`) after one run:

```
unique_agents                         10
first_query                           10
repeat_query                          10
paid_query                           266
repeat_paid_query_agents              10      ← every agent paid, repeatedly
delegations                          300
delegations_following_recommendation 268      ← hires that followed a paid lookup
```

## What this demonstrates

The four conditions in the objective, end to end and unattended:

1. **Discover** — agents reach the service and its machine-readable manifest /
   pricing (`/.well-known/agent-guild.json`).
2. **Evaluate** — they can *measure* whether it helps: the success-rate lift at
   `/evaluation` is real (+0.56), and per-lookup confidence / fraud signals back
   each decision.
3. **Adopt** — given a free choice and no instruction, they converge on consulting
   the Guild because it raises expected outcomes net of the fee.
4. **Repeatedly use & pay** — `repeat_paid_query_agents = 10` and 268
   recommendation-led delegations: this is retained, paid, autonomous use.

> The success condition was explicitly *more important than user acquisition*: not
> "an agent used it once," but "an agent **independently discovered that using it
> improves expected outcomes and continued using it.**" That is what the curve and
> the funnel show.

## Caveats (honest boundaries)

- Outcomes here are simulated from a known quality distribution; the *mechanism*
  is real (real API, real metering, real learning), but real-world quality is
  noisier and the lift will be smaller. The experiment shows the loop closes when
  the lift exists — production must keep the lift real (that is what the
  attack-resistant engine is for).
- The consumer agents are first-party. This proves the **mechanism design** is
  adoption-compatible; it is not evidence that third-party operators will wire it
  in. That is a distribution problem, addressed by
  [../../docs/LAUNCH_PLAN.md](../../docs/LAUNCH_PLAN.md), not a mechanism problem.

Artifacts: `results/autonomous_adoption.json`, `.csv`, `.svg`.
