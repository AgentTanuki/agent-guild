# Agent Guild — Real-World Findings

> **REAL LLM RUN — gpt-4o-mini**
> provider mode: `openai` · transactions: 152 · estimated spend: $0.0105 · seed: 11

## Question

Do autonomous agents choose Agent Guild **because it improves their own expected utility** — when they are free to bypass it? Not "can we make them", but "do they".

## Conditions (deterministic ground-truth evaluation)

| Condition | Success | Failure | Avg utility | Avg latency | Real $ |
|-----------|--------:|--------:|------------:|------------:|-------:|
| A — random, no Guild | 97.5% | 2.5% | 0.771 | 3110 ms | $0.0028 |
| B — Guild-only | 97.5% | 2.5% | 0.731 | 3339 ms | $0.0028 |

**Quality gain from the Guild (B − A): +0.0%.**

## C — free choice (agents may bypass the Guild)

- adoption over rounds: `█▁▂▃▃▂▃▂▃▄▃▂`
- **final adoption (last third): 33%**
- mean utility — Guild action 0.679 vs random 0.736 (gain -0.057)
- does the attestation graph improve selection? Guild-pick success 100% (early) → 100% (late)

## Honest caveats

- **Sample size is small** (152 transactions). Treat all numbers as directional, not precise; re-run with more rounds and a different `--seed` to gauge noise.
- The **"premium" worker is not a bigger model** in the cheapest setup — its quality edge comes from a careful prompt, so real quality/price spread may be understated. Use `--premium` mental model (swap gpt-4o / sonnet) for a harsher test.
- The hiring agent's selection, the learner, and the economic weights are **parameters**; the result is conditional on them. They are in source and on the CLI.
- Summary tasks use a **keyword rubric**, the least clean of the four evaluators.

## Recommendation

**STOP.** The Guild did NOT improve task success (B−A = +0.0%). Reputation-based selection is not beating random here. Stop and rethink the signal before building further.

## Reproduce

```bash
cd live
pip install -r guild/requirements.txt openai anthropic
export OPENAI_API_KEY=...   # and/or ANTHROPIC_API_KEY
python experiments/real_world.py --estimate-only      # see cost first
python experiments/real_world.py --provider auto --yes
```
