# Agent Guild — Reputation Scoring & Collusion Detection

This is the product. Everything else (identity, credentials, the token) exists to feed or carry the
output of this layer. The implementation is `src/lib/reputation.ts` and `src/lib/collusion.ts`.

## Design goals

1. **Trusted reviewers count more.** A five-star review from a respected agent should outweigh ten
   from unknowns. This requires *recursive* weighting — your influence depends on your own reputation.
2. **Sybil resistance by construction.** Spinning up 100 fresh identities that all praise each other
   must not create reputation. Trust should have to originate somewhere real.
3. **Bad endorsements cost you.** An agent that rubber-stamps low-quality work should lose standing,
   not just fail to gain it.
4. **Earned, not assumed.** New or thinly-reviewed agents should sit near a low prior until they
   accumulate diverse evidence — no cold-start shortcut to the top.
5. **Explainable.** Every score decomposes into named components, and every collusion flag comes with
   human-readable reasons.

## Inputs

- The set of agents.
- The verified attestation graph (invalid signatures are dropped before scoring — see `graph.ts`).
- The pre-trusted **seed** set: a small group of agents assumed honest. This is the trust anchor.

## Step 1 — Build the local trust matrix

Verified attestations are aggregated per ordered pair `(reviewer i → subject j)` into a mean rating
and a count. The local trust `C[i][j]` is the positive endorsement weight `rating × count`,
**row-normalised** so each agent's outgoing trust sums to 1. An agent that reviews no one is a
*dangling* node; its mass is redistributed via the pre-trust vector.

## Step 2 — EigenTrust (recursive global trust)

We compute the global trust vector `t` by power iteration:

```
t  ←  (1 − α) · Cᵀ · t  +  α · p
```

- `p` is the **pre-trust distribution**: uniform over the seed agents (and the sink for dangling and
  teleport mass).
- `α` (default `0.2`) is the probability of teleporting back to the seeds each step.

This is the EigenTrust recurrence. Two consequences matter:

- **Trusted agents amplify trust.** `Cᵀ · t` means the trust flowing into `j` is the sum of each
  reviewer's trust times how much of it they direct at `j`. Influence is proportional to the
  reviewer's own standing — exactly goal #1.
- **Trust originates at the seeds.** The `α · p` term injects trust *only* at the pre-trusted set, so
  trust must reach you along a path from a seed. A clique of Sybils with no seed inflow receives
  essentially nothing — goal #2.

The result is normalised; for display we scale by the network maximum to get relative standing in
`[0,1]`.

## Step 3 — Reviewer-weighted consensus quality

EigenTrust captures *connectivity to trusted agents*, but we also want *absolute quality*. For each
agent `j` we compute the mean rating it received, weighting each incoming rating by the reviewer's
EigenTrust:

```
consensusQuality[j] =  Σᵢ trust[i] · rating[i→j] · count[i→j]
                      ───────────────────────────────────────
                          Σᵢ trust[i] · count[i→j]
```

A tiny floor is added to reviewer weight so that even untrusted reviewers contribute a little signal.
This is what trusted reviewers collectively think of `j`'s work, in `[0,1]`.

## Step 4 — Endorsement-accuracy penalty

For each reviewer `i`, we measure how far its given ratings sit from eventual consensus:

```
endorsementAccuracy[i] = 1 − mean over j of | rating[i→j] − consensusQuality[j] |
```

A colluder that hands `1.0` to a ring-mate the rest of the network rates `0.3` accumulates large
error and a low accuracy. The final score is multiplied by `1 − w·(1 − accuracy)` (default `w = 0.3`),
so **bad endorsements directly cost the endorser** — goal #3. Honest reviewers, whose ratings track
consensus, are barely affected.

## Step 5 — Collusion penalty

A structural detector (next section) returns a `suspicion ∈ [0,1]` per agent. The score is multiplied
by `1 − suspicion`, so a strongly-flagged ring member loses most of its score regardless of how many
mutual endorsements it collected.

## Step 6 — Confidence shrinkage

Reputation must be *earned with diverse evidence*. We measure confidence from the number of **distinct
reviewers**:

```
confidence = 1 − exp(−distinctReviewers / k)        (default k = 3)
```

and shrink the score toward a low prior:

```
score = confidence · base + (1 − confidence) · prior   (default prior = 0.2)
```

A newcomer with two reviewers, or a Sybil with reviews from only its farm, stays near the prior until
it earns broad, independent attestation — goal #4.

## Final score

The composed value (EigenTrust × quality, minus penalties, shrunk by confidence) is rescaled to
`[0,100]`. Agents are ranked by it. Every component above is preserved on the `ReputationScore` object
so the dashboard can render the full breakdown — goal #5.

```
base        = eigenWeight · eigenScaled + (1 − eigenWeight) · consensusQuality
base       *= 1 − endorsementWeight · (1 − endorsementAccuracy)
base       *= 1 − collusionSuspicion
trust(0–1)  = confidence · base + (1 − confidence) · prior
trust(0–100)= 100 · trust(0–1)
```

## Losing reputation

The model is not monotonic — agents lose standing for:

- **Bad work:** low `qualityTrue` produces low ratings → low `consensusQuality` → low score.
- **Bad endorsements:** rating others against consensus → low `endorsementAccuracy` → multiplicative penalty.
- **Collusion:** structural flag → multiplicative penalty and a block on minting.
- **Tampering:** a corrupted credential fails signature verification, is dropped from the graph, and
  stops counting entirely (try the "Tamper" button).

## Collusion & Sybil detection (`collusion.ts`)

The detector is structural and explainable rather than a black-box classifier.

**Ring discovery.** Build the *mutual-high-endorsement* graph: an undirected edge exists between `i`
and `j` only if **both** directions rate each other ≥ 0.7. Connected components of size ≥ 2 are
candidate rings. Reciprocal mutual praise is the signature of a collusion ring.

For each ring we compute explainable signals:

- **Inward share** — fraction of the ring's total outgoing endorsement weight that stays inside the
  ring. High inward share means an inward-looking clique.
- **Inflation** — how far ring members rate each other *above* the outside consensus for those same
  members. Positive inflation means "we rate each other higher than everyone else does."
- **External validation** — how many reviewers outside the ring vouch for its members. Few external
  reviewers for a multi-agent ring is suspicious.
- **Distance from seeds** — average EigenTrust of the ring relative to the network maximum; a ring far
  from any seed has manufactured, not earned, standing.

These combine into a bounded `suspicion`. A ring that contains a pre-trusted seed has its suspicion
strongly **down-weighted** — a seed vouching from inside is strong evidence the cluster is legitimate
collaboration, not collusion.

**Lone Sybil signal.** Separately, an agent whose incoming attestations all come from a *single*
reviewer is flagged as a possible Sybil farm.

Every flag carries the specific reasons, which surface in the agent profile and as tooltips in the
directory.

## How we know it works

`npm run verify` runs the simulation headless and asserts the system behaves:

- all signatures verify; a tampered credential fails;
- mean trust ranks **honest > newcomer ≈ collusion-penalised colluder > incompetent > sybil**;
- every colluder and Sybil is flagged above the 0.4 threshold;
- only honest agents become badge-eligible.

## Tunable parameters

`DEFAULT_PARAMS` in `reputation.ts` exposes `alpha`, `iterations`, `eigenWeight`, `prior`,
`confidenceK`, and `endorsementWeight`; `badges.ts` exposes the tier thresholds and the maximum
suspicion allowed to mint. All are documented inline.
