# Phase 3 — Findings

**Question:** Do autonomous agents *voluntarily* converge on Agent Guild as a trust
mechanism when they are free to ignore it?

**Answer (under realistic economic parameters): yes.** Free-choice agents start out
mostly ignoring the Guild, and converge to using it ~4 out of 5 times once the
attestation graph has filled in enough to be informative — because doing so measurably
improves their own utility.

The numbers below are from the offline deterministic backend (reproducible, no API key).
With a real provider (`--provider openai`/`anthropic`) the same dynamics are driven by
genuine model-accuracy differences; absolute numbers will vary, the structure should not.

## Part A — comparative baseline (120 real transactions each)

| Environment | Success | Failure | Avg net utility | Notes |
|-------------|--------:|--------:|----------------:|-------|
| **A — no Guild** (random selection) | 65.8% | 34.2% | **0.124** | picks blind; lands on value-traps a third of the time |
| **B — Guild** (reputation selection) | 93.3% | 6.7% | **0.768** | even after paying the verification cost |

Selecting through the Guild cut the failure rate from 34% to 7% and produced **~6×**
the net utility. Note Env B's average price (0.045) is *not* the cheapest worker: the
agent learned to pay for the best **value** (a reliable mid-priced fact-checker), not the
cheapest (a "value trap") nor the most expensive — genuine economic discrimination.

## Part B — free-choice convergence (300 transactions, 10 agents × 30 rounds)

Agents were free each round to consult the Guild (small query cost) or pick at random
(free, blind), learning from realized utility.

```
adoption over rounds:  █▁▃▃▁▄▃▄▃▃▃▅▄▅▅▅▅▅▅▅▅▆▆▅▅▆▆▆▆▆
first rounds: [1.00, 0.00, 0.30, 0.40, 0.10]   ← cold start: Guild graph still empty
last rounds:  [0.80, 0.80, 0.80, 0.80, 0.80]   ← converged
```

- **Final adoption (last third): 77%.**
- **Mean utility — Guild action 0.650 vs random action 0.233** (gain **+0.417**).
- **Verdict: ✅ agents voluntarily converge.**

### The shape of the curve is the whole story

Round 1 every agent tries the Guild — and it underperforms, because the attestation
graph is *empty* and selection is no better than random while still costing a query fee.
For several rounds adoption dips as agents sour on it. But every transaction — Guild or
random — deposits a signed attestation, so the graph fills regardless. Around the midpoint
the Guild becomes informative, Guild-actions start clearly out-earning random-actions, and
the recency-weighted learners shift over. Adoption climbs and holds at ~80%.

This is the **cold-start → network-effect** dynamic made concrete: the product is weak
until usage makes it strong, after which rational agents adopt it without being told to.
It also shows why a naive learner can *fail* to converge (we observed exactly this before
tuning): if agents only sampled the Guild once, early, on the empty graph, they could lock
onto "random" forever. Convergence requires (a) the graph to fill and (b) agents that keep
re-evaluating — both of which hold here.

## What would make the answer "no"

The harness reports honestly and *did* print ❌ in under-powered configurations. Adoption
fails to emerge when: the verification cost exceeds the accuracy benefit; wrong answers are
no costlier than right answers are valuable (so quality barely matters); too few rounds for
the graph to mature; or a learner that never revisits an early bad experience. These are the
real design constraints for shipping the network, not incidental knobs.

## Parameters (in `experiments/phase3.py`)

`reward=+1.0`, `penalty=−1.5` (a wrong fact-check corrupts downstream work, so it is costlier
than a right one is valuable), `latency_cost=0.05/s`, `guild_query_cost=0.02`. Worker pool of
six fact-checkers spanning high/medium/low reliability with prices 0.004–0.05 and advertised
latencies 250–1500 ms, including deliberate value-traps. Learner: recency-weighted
(α=0.3) epsilon-greedy with optimistic init and decaying exploration. All are CLI/​source
configurable; re-run with `--provider`, `--rounds`, `--consumers` to probe robustness.

## Bottom line

When the Guild improves outcomes, self-interested agents choose it on their own — and the
mechanism that makes it improve outcomes (more usage → a richer attestation graph → better
discovery) is the same mechanism that makes it defensible. That is the evidence of
product-market fit the experiment set out to find.
