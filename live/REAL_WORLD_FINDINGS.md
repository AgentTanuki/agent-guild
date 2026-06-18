# Agent Guild — Real-World Findings

> **REAL LLM RUN — gpt-4o, gpt-4o-mini**
> provider mode: `openai` · transactions: 152 · estimated spend: $0.0926 · seed: 11

## Question

Do autonomous agents choose Agent Guild **because it improves their own expected utility** — when they are free to bypass it? Not "can we make them", but "do they".

## Conditions (deterministic ground-truth evaluation)

| Condition | Success | Failure | Avg utility | Avg latency | Real $ |
|-----------|--------:|--------:|------------:|------------:|-------:|
| A — random, no Guild | 87.5% | 12.5% | 0.487 | 3490 ms | $0.0309 |
| B — Guild-only | 95.0% | 5.0% | 0.730 | 2345 ms | $0.0082 |

**Quality gain from the Guild (B − A): +7.5%.**

## C — free choice (agents may bypass the Guild)

- adoption over rounds: `█▁▂▃▄▃▃▂▃▅▃▂`
- **final adoption (last third): 38%**
- mean utility — Guild action 0.182 vs random 0.604 (gain -0.422)
- does the attestation graph improve selection? Guild-pick success 88% (early) → 88% (late)

## Honest caveats

- **Sample size is small** (152 transactions). Treat all numbers as directional, not precise; re-run with more rounds and a different `--seed` to gauge noise.
- The **"premium" worker is not a bigger model** in the cheapest setup — its quality edge comes from a careful prompt, so real quality/price spread may be understated. Use `--premium` mental model (swap gpt-4o / sonnet) for a harsher test.
- The hiring agent's selection, the learner, and the economic weights are **parameters**; the result is conditional on them. They are in source and on the CLI.
- Summary tasks use a **keyword rubric**, the least clean of the four evaluators.

## Recommendation

**REDESIGN.** The Guild improved success by +7.5% but free agents did NOT converge (adoption 38%, utility gain -0.422). The signal exists but is too weak/slow to drive adoption. Redesign incentives or learning before scaling.

## Reproduce

```bash
cd live
pip install -r guild/requirements.txt openai anthropic
export OPENAI_API_KEY=...   # and/or ANTHROPIC_API_KEY
python experiments/real_world.py --estimate-only      # see cost first
python experiments/real_world.py --provider auto --yes
```
