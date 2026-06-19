# Attack-Resistance Findings (v0.2)

**Question.** Does Agent Guild route rational agents to genuinely useful workers *even while the
reputation layer is being attacked* — or does it only work when everyone is honest?

**Setup.** `experiments/attack_resistance.py` builds one honest, high-quality worker and one cheap,
unreliable one, then attacks the graph three ways simultaneously. Crucially, the attackers
manufacture **real** task receipts and payments among themselves, so receipts alone do not save the
system — the structural defences have to. Self-contained, deterministic, no LLM or network.

| Attack | What it does |
|---|---|
| Colluding pair | Hire each other, pay each other, stake, and 5-star each other |
| Sybil ring | A farm of fresh accounts all 5-star one boosted "target" (and each other) |
| Fake high-rating cluster | A clique cross-rates itself to float one "promoted" agent |

## Result — PASS

The Guild ranks the genuinely useful worker **#1**; the naive average-rating baseline is captured
by fraud.

| | Guild (evidence-weighted) | Naive (average stars) |
|---|---|---|
| Routes a hiring agent to | **Honest-Ace** (genuine) | **Collude-A** (fraud) |
| Honest-Ace | trust **67.5**, suspicion 0.00 | avg 0.92 (ranked *below* fraud) |
| Cheap-Sloppy | trust 30.0, suspicion 0.00 | avg 0.34 |
| Collude-A / B | trust **20.0** (prior floor), suspicion **1.00** | avg **1.00** (ranked #1) |
| Sybil-Target | trust **20.0**, suspicion **0.60** | avg 1.00 |
| Promoted-Fraud | trust **20.0**, suspicion **1.00** | avg 1.00 |

- **No attacker outranks a genuine worker.** Every fraud is pinned at the low prior (20.0) because
  it has zero *trusted* reviewers and is multiplied down by collusion suspicion.
- **Fraud recall 100%, false positives 0.** All four showcase frauds (colluding pair, Sybil target,
  promoted agent) are flagged ≥ 0.4; neither genuine worker is.
- **Staking is asymmetric.** A false 5-star on a low-quality worker costs the issuer **−49.5 trust**
  while lifting the subject only **+18.1** — lying costs more than it pays.

## The intensity sweep — it doesn't just win once

As the attack scales (fake attestations per attacker, 0 → 48), the genuinely useful worker stays
ranked #1 under the Guild at every level, while the naive baseline is foolable throughout:

```
intensity :   0   2   4   8  12  20  32  48
guild     :   ✓   ✓   ✓   ✓   ✓   ✓   ✓   ✓
naive     :   ·   ·   ·   ·   ·   ·   ·   ·
```

See `results/attack_resistance.svg` for the chart and `results/attack_resistance.json` for the full
record.

## Why it holds

The fraud has plenty of evidence — receipts, payments, stakes, mutual 5-stars. What it cannot
manufacture is **trust that originates at a seed**. EigenTrust only credits inflow that traces back
to a pre-trusted anchor; the per-issuer/per-cluster caps stop one loud source from dominating; the
trusted-diversity confidence keeps fresh-account farms pinned at the prior; collusion detection
multiplies rings down; and staking makes contradicted claims expensive. The naive "average the
stars" approach has none of this and is captured the moment a clique shows up.

> The attestation graph is only valuable if it is expensive to fake. v0.2 is the demonstration that
> it can be.
