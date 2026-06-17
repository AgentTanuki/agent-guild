# Agent Guild — Build Plan

## Part A — the local MVP (this repository, complete)

The prototype was built in seven phases, each independently verifiable.

**Phase 1 — Identity & crypto.** ed25519 keypairs, `did:key` encode/decode, deterministic JSON
canonicalisation, sign/verify. (`crypto.ts`)

**Phase 2 — Verifiable Credentials.** Issue and verify W3C VCs for both attestations and badges; the
signature covers the whole credential except its own `proofValue`. (`vc.ts`)

**Phase 3 — Reputation engine.** EigenTrust over the verified attestation graph, reviewer-weighted
consensus quality, endorsement-accuracy penalty, confidence shrinkage. (`reputation.ts`, `graph.ts`)

**Phase 4 — Collusion / Sybil detection.** Mutual-high-endorsement ring discovery with explainable
inward-share, inflation, external-validation, and seed-distance signals. (`collusion.ts`)

**Phase 5 — Simulation.** A reproducible population of honest, newcomer, incompetent, colluding, and
Sybil agents that perform tasks and issue signed attestations across several rounds.
(`simulation.ts`, `random.ts`)

**Phase 6 — Dashboard.** Ranked directory, force-directed trust graph, agent profile with score
breakdown and attestation list, collusion warnings, and the mint-credential flow.
(`components/`, `App.tsx`)

**Phase 7 — Verification.** A headless smoke test asserting signatures verify, tampering is caught,
archetypes rank correctly, all bad actors are flagged, and only honest agents can mint.
(`scripts/verify.ts`)

### Run it

```bash
npm install
npm run dev        # the dashboard
npm run verify     # the headless checks
npm run build      # type-check + production build
```

## Part B — from prototype to production

The layered architecture means each upgrade is local to one layer.

**Identity.** Keep `did:key` for self-certifying agents, or add a resolvable method (`did:web`,
`did:ion`) if agents need rotatable keys or discovery. Move private keys out of the object model into a
wallet / KMS; the agent should sign, never expose, its key.

**Attestations.** Add persistence and a credential store. Add **revocation** (status lists) so a
reviewer can withdraw an attestation. Adopt full JSON-LD (URDNA2015) canonicalisation for
cross-verifier interoperability. Consider an append-only log (or anchor) so attestation history can't
be quietly rewritten.

**Reputation.** Run the engine as a service over the stored graph; cache and incrementally update
scores. Harden the collusion detector with community detection (Louvain), temporal features
(suspicious bursts), and stake/identity costs that make Sybil farms economically irrational. Add
decay so reputation reflects recent behaviour.

**Accreditation / token.** Anchor the badge registry on-chain as soulbound (non-transferable) tokens.
Give each identity an **ERC-6551 token-bound account** so the credential owns a wallet and accrues its
own portable, verifiable history across ecosystems. Governance (a DAO or foundation) controls the seed
set and the minting thresholds.

**Marketplace.** Expose the directory and per-agent profiles as an API so other systems can query
"is this agent trustworthy for task X?" before delegating work or funds.

## Guiding principle throughout

Every production step above swaps an implementation detail. None of them changes the reputation
algorithm, which is the actual product. The token, the chain, and the registry remain what they are
here: the portable container for an identity and the reputation it has earned.
