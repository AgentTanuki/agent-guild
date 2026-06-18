# Agent Guild — Adoption-Sensitivity Findings

**Question.** Under what conditions do autonomous agents *voluntarily* adopt the
Guild — and under what conditions does adoption fail? We vary four factors
**independently** and measure adoption, utility, success, cost, and convergence.
We did **not** tune anything to make the Guild win; we report failure as readily
as success.

## Method

Runs on the **real EigenTrust reputation engine** (`app.reputation.score_agents`)
and faithfully reuses the production hiring-agent selection rule, the
recency-weighted Bandit learner, and the economic model. Only worker *quality* is
synthetic — it must be, because **task difficulty** and **quality spread** cannot
be set as independent variables with real LLMs. Worker price is correlated with
quality plus noise, so the cheapest worker is usually (not always) the worst —
genuine cheap-good "value" workers and pricey-bad "traps" both occur, and the
Guild query is always a real utility cost.

Three conditions per cell: **A** = forced random selection, **B** = forced
Guild selection, **C** = free choice (each consumer learns whether to consult the
Guild). Baseline: query_cost 0.02, learner α 0.3 / ε₀ 0.3, difficulty 0.15,
spread 0.5, 6 workers, 40 rounds × 8 consumers, averaged over **32 seeds**.

**Metrics.** `final_adoption` (last-third of the C curve), `adoption_trend`
(final − early, i.e. did adoption grow or collapse), `util_gain` (Guild-action
minus random-action utility in C), `B−A` (Guild vs random *success*), and
`settling_round` (convergence speed).

## Headline

Voluntary adoption is gated by one thing: **is the reputation signal worth more
than its cost?** The dominant lever is **worker quality spread** (is there
anything to learn?), then **task difficulty/stakes** (does being right matter?),
then **query cost** and **learner design**. When workers are indistinguishable
or tasks are trivial, rational agents correctly *decline* to adopt the Guild —
that is the right behaviour, not a defect.

A sharp validation that nothing is rigged: at **spread = 0** the Guild's success
advantage is **B−A ≈ 0.00** and free agents do not adopt. The Guild only earns
adoption when it can actually tell good workers from bad.

## 1. Worker quality spread — the dominant driver

(difficulty fixed at 0.15)

| spread | final adoption | util gain | B−A success |
|-------:|---------------:|----------:|------------:|
| 0.0 | 0.49 | −0.066 | −0.00 |
| 0.1 | 0.48 | −0.072 | −0.01 |
| 0.2 | 0.39 | −0.130 | +0.01 |
| 0.4 | 0.49 | −0.043 | +0.05 |
| 0.6 | 0.59 | **+0.138** | +0.11 |
| 0.8 | 0.76 | **+0.490** | +0.15 |

Adoption and utility gain are flat/negative until spread clears ~0.4–0.6, then
climb steeply. Note the **dip at spread 0.2**: a weak signal is worse than none —
agents act on noisy reputation, get burned, and adopt *less* than when workers
are identical. Reputation must be clearly informative to pay for itself.

## 2. Task difficulty — a multiplier on the signal

(spread fixed at 0.5)

| difficulty | final adoption | util gain | B−A success |
|-----------:|---------------:|----------:|------------:|
| 0.00 (ceiling) | 0.54 | −0.018 | +0.01 |
| 0.10 | 0.60 | +0.113 | +0.07 |
| 0.20 | 0.55 | +0.086 | +0.09 |
| 0.30 | 0.56 | +0.092 | +0.07 |
| 0.45 (hard) | 0.62 | **+0.288** | +0.08 |

At **zero difficulty** everyone succeeds, so good selection is worthless and the
gain is negative — this reproduces the real-LLM `gpt-4o-mini` null result
exactly. As failure becomes more likely and costly, the value of picking a
reliable worker rises and adoption follows.

## 3. Guild query cost — a real, monotone drag

| query cost | final adoption | util gain | B−A success |
|-----------:|---------------:|----------:|------------:|
| 0.00 | 0.56 | +0.116 | +0.08 |
| 0.01 | 0.53 | +0.075 | +0.08 |
| 0.02 | 0.54 | +0.022 | +0.08 |
| 0.05 | 0.53 | +0.019 | +0.08 |
| 0.10 | 0.49 | −0.027 | +0.08 |
| 0.20 | 0.50 | −0.035 | +0.08 |

The Guild's *quality* edge (B−A) is unchanged — but voluntary adoption erodes as
the query gets expensive, flipping the utility gain negative beyond ~0.05–0.10.
Cheap or free discovery is materially important to adoption.

## 4. Learner design — low exploration wins; don't over-update

| ε₀ (exploration) | adoption | gain | | α (aggressiveness) | adoption | gain |
|---:|---:|---:|---|---:|---:|---:|
| 0.05 | 0.56 | +0.108 | | 0.1 | 0.53 | +0.022 |
| 0.10 | **0.62** | **+0.136** | | 0.2 | 0.52 | +0.030 |
| 0.20 | 0.54 | +0.070 | | 0.3 | 0.54 | +0.022 |
| 0.30 | 0.54 | +0.022 | | 0.5 | 0.52 | +0.019 |
| 0.50 | 0.58 | +0.080 | | 0.8 | 0.47 | +0.001 |

Lower exploration (ε₀ ≈ 0.1) lets agents commit once they learn the Guild is
better; high exploration keeps sampling random and suppresses adoption. Very
aggressive updating (α 0.8) chases noise and lowers adoption. Convergence
*speed* (`settling_round` ≈ 27–33) was nearly constant across every cell — what
changes is the **level** adoption settles at, not how fast it gets there.

## 5. Difficulty × spread interaction (final adoption / util gain)

| difficulty ↓ \ spread → | 0.0 | 0.2 | 0.4 | 0.7 |
|---:|:--:|:--:|:--:|:--:|
| 0.00 | 0.48 / −0.03 | 0.39 / −0.15 | 0.47 / −0.13 | 0.67 / **+0.23** |
| 0.15 | 0.49 / −0.07 | 0.39 / −0.13 | 0.49 / −0.04 | 0.65 / **+0.27** |
| 0.30 | 0.49 / −0.01 | 0.46 / −0.07 | 0.54 / −0.00 | 0.60 / **+0.23** |
| 0.45 | 0.47 / −0.04 | 0.51 / +0.03 | 0.54 / **+0.13** | 0.67 / **+0.39** |

Spread is necessary; difficulty is a multiplier. Adoption only crosses into the
self-sustaining region (final > 0.6, positive gain) when spread is high (~0.7).
Difficulty sharpens the effect at a given spread but cannot rescue a
low-spread world — if workers are interchangeable, no amount of task difficulty
makes the Guild worth consulting.

## Where adoption EMERGES vs FAILS

**Emerges** (voluntary, self-sustaining): genuine quality spread (s ≳ 0.6),
meaningful task stakes (costly failures), cheap discovery (query cost ≲ 0.05),
and a learner that exploits rather than over-explores. Best cell reached ~0.76
adoption with a +0.49 utility gain.

**Fails** (and rationally so): indistinguishable workers (low spread), trivial
tasks (ceiling effect), expensive queries, or high-exploration learners. A
weak-but-nonzero signal (spread ≈ 0.2) is the worst case — actively misleading.

## Honest caveats

- Even the best cells settle at ~0.76–0.81 adoption, **not** ~1.0. Under this
  recency-weighted ε-greedy learner, winner-take-all adoption does **not**
  emerge — there is a persistent exploration floor. Pushing toward universal
  adoption is a **learner/incentive-design** problem, not a reputation-quality
  problem.
- Worker quality is synthetic and Bernoulli; real LLM error is correlated across
  tasks and worker types, which this does not model. Treat the *directions and
  thresholds* as the result, not the exact numbers.
- The selection rule, learner, and economic weights are the production defaults;
  conclusions are conditional on them (they are the variables in §3–4).

## Reproduce

```bash
cd live
python experiments/adoption_sweep.py --axis all --seeds 32   # all factor axes + grid
python experiments/adoption_sweep.py --axis baseline --seeds 32
python experiments/adoption_sweep.py --report                 # -> results/adoption_sweep.csv
```

Raw per-cell data: `experiments/results/sweep_*.json` and
`experiments/results/adoption_sweep.csv`.
