# Agent Guild — Real-World Findings

> **⚠ OFFLINE SELF-VALIDATION (deterministic backend, NOT a real-world result)**
> provider mode: `mock` · transactions: 152 · estimated spend: $0.0000 · seed: 11

## Question

Do autonomous agents choose Agent Guild **because it improves their own expected utility** — when they are free to bypass it? Not "can we make them", but "do they".

## Conditions (deterministic ground-truth evaluation)

| Condition | Success | Failure | Avg utility | Avg latency | Real $ |
|-----------|--------:|--------:|------------:|------------:|-------:|
| A — random, no Guild | 80.0% | 20.0% | 0.455 | 673 ms | $0.0000 |
| B — Guild-only | 90.0% | 10.0% | 0.657 | 1084 ms | $0.0000 |

**Quality gain from the Guild (B − A): +10.0%.**

## C — free choice (agents may bypass the Guild)

- adoption over rounds: `█▁▂▄▃▄▅▆▆▅▅▆`
- **final adoption (last third): 75%**
- mean utility — Guild action 0.672 vs random 0.206 (gain +0.467)
- does the attestation graph improve selection? Guild-pick success 93% (early) → 93% (late)

## Honest caveats

- **Sample size is small** (152 transactions). Treat all numbers as directional, not precise; re-run with more rounds and a different `--seed` to gauge noise.
- The **"premium" worker is not a bigger model** in the cheapest setup — its quality edge comes from a careful prompt, so real quality/price spread may be understated. Use `--premium` mental model (swap gpt-4o / sonnet) for a harsher test.
- The hiring agent's selection, the learner, and the economic weights are **parameters**; the result is conditional on them. They are in source and on the CLI.
- Summary tasks use a **keyword rubric**, the least clean of the four evaluators.
- **These specific numbers are from the offline backend** and exist only to prove the harness runs end to end. They are NOT evidence about real model behaviour.

## Recommendation

**VALIDATION-ONLY.** Offline self-validation only — no real LLM calls were made. Run with a provider key and --yes to obtain a real-world result before deciding.

## Reproduce

```bash
cd live
pip install -r guild/requirements.txt openai anthropic
export OPENAI_API_KEY=...   # and/or ANTHROPIC_API_KEY
python experiments/real_world.py --estimate-only      # see cost first
python experiments/real_world.py --provider auto --yes
```
