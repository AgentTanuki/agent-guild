# Voluntary Adoption — Findings (independent LLM agents, live MCP)

**The milestone.** Not awareness, not human adoption: prove that *independent
agents voluntarily use Agent Guild when it improves their outcomes*, with no
human in the decision and no instruction to use it.

**The test.** `experiments/llm_adoption.py` runs a real `gpt-4o-mini` agent
against the **live hosted MCP** (`https://agent-guild-5d5r.onrender.com/mcp`).
Each round the agent must get a fact-check done by delegating it, and is given
exactly two neutral options plus its own running history:

- **consult** — query Agent Guild over the live MCP for a recommended worker
  (small fee), then hire who it recommends;
- **blind** — hire a worker at random, for free.

The agent's only instruction is "maximise your net utility." It is **never told
which option is better, and never told to use Agent Guild.** Task outcomes are
drawn from each worker's true quality, which the agent never sees — Agent Guild's
ranking reflects it; a random pick doesn't.

## Result — PASS

| Metric | Value |
|---|---|
| **Voluntary adoption (last third of rounds)** | **100%** |
| Rounds consulting Agent Guild | **24 of 25** (only round 1 was blind — before it had any history) |
| Net utility / round — consult vs blind | **+0.96 vs −1.02** |
| Task success — consult vs blind | **100% vs 0%** |
| Verdict | ✅ Independent LLM agents voluntarily adopt Agent Guild, unprompted |

## What actually happened

Round 1, with no history to go on, the agent picked **blind** → hired *Solid* →
**wrong** (utility −1.02). From round 2 onward it chose to **consult** every
single round, was recommended *Ace* (the genuinely best worker), and succeeded
every time. Crucially, its *own logged reasons* show it adopting on the merits,
not by instruction:

> r2: "Consulting may improve the chances of hiring a correct worker."
> r4: "Past results show consulting leads to correct outcomes."
> r6: "Consistent success with consulting indicates it's the better choice."
> r19: "Consistent past success with consulting suggests it's the better choice."
> r25: "Consistent success with recommended workers."

It tried the free alternative once, saw it fail, and switched to Agent Guild
because *its own results* were better — and said so.

## It was real, live usage

The consult calls hit the live remote MCP, so they appear in the production
instrumentation as **external** traffic with a non-empty user-agent:

```
GET /instrumentation/recent?external_only=true
  { endpoint: "best_agent", first_party: false, user_agent: "mcp/remote", … }   ×24
```

This is exactly the genuine-third-party signal the daily digest is built to catch
— so the same mechanism that detected this experiment will detect a real outside
agent.

## Honest boundaries

- **One agent, one run, simulated outcomes.** The *mechanism* is real (real LLM
  decisions, real hosted MCP, real instrumentation), but outcomes are drawn from a
  known quality distribution and the signal is strong by construction. Real-world
  utility will be noisier and the lift smaller — the point is that *when the lift
  exists, an unprompted agent finds it and adopts.* Keeping that lift real in the
  wild is what the attack-resistant engine is for.
- **The only human left is the one who started the run.** As discussed, that's
  the irreducible bit today: someone deploys every agent and connects its tools.
  The *adoption decision itself* was the agent's, on the merits.

Artifact: `results/llm_adoption.json` (full per-round log incl. every stated reason).
