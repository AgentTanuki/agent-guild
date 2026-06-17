# Agent Guild — Architecture

## 1. Purpose and framing

Agent Guild is a reputation layer for autonomous AI agents. The thesis is that as agents begin to
delegate work to each other, they need the same thing human professionals need: a persistent
identity, a verifiable track record, and a portable reputation that other parties can check before
trusting them with a task or with money.

The deliverable here is a **local prototype**. It models the full conceptual stack — identity,
attestation, reputation, accreditation — using real cryptography but no blockchain. Everything runs
in the browser. The architecture is deliberately layered so that each layer could later be swapped
for a production implementation (a DID resolver, an on-chain registry, a token-bound account)
without rewriting the layers above it.

## 2. Layered model

The system has four conceptual layers. The lower layers know nothing about the higher ones.

**Identity layer.** Each agent is a keypair. The public key is encoded as a `did:key` DID, which is
self-certifying: anyone can derive the verification key directly from the identifier, with no
registry lookup. This is the root of every signature in the system.

**Attestation layer.** Work and reviews are expressed as W3C Verifiable Credentials. A review is a
credential issued by the reviewer's DID, asserting a quality rating about a task performed by the
subject's DID, signed with the reviewer's private key. Because the credential is signed, it is
non-repudiable and tamper-evident: change one field and verification fails.

**Reputation layer (the product).** A scoring engine consumes the graph of verified attestations and
produces a trust score per agent. This is where the intelligence lives: recursive trust propagation,
endorsement-accuracy penalties, collusion detection, and confidence weighting. Nothing in this layer
trusts a raw rating at face value — a rating only matters in proportion to how trusted its author is.

**Accreditation layer.** When an agent crosses defined thresholds, the Guild authority issues a
soulbound credential — itself a Verifiable Credential, bound to the agent's DID, with no transfer
semantics. This is the portable container the agent carries between contexts.

## 3. Components

The codebase maps one-to-one onto the layers.

```
src/lib/
  crypto.ts        ed25519 keys, did:key encode/decode, canonicalisation, sign/verify
  vc.ts            issue & verify W3C Verifiable Credentials
  types.ts         domain model (Agent, Task, Attestation, Badge, scores…)
  graph.ts         aggregate verified attestations into a weighted trust graph
  reputation.ts    EigenTrust + consensus quality + penalties + confidence  ← the product
  collusion.ts     structural Sybil / collusion detector
  badges.ts        threshold evaluation + soulbound credential minting
  simulation.ts    generates the agent population, tasks, and signed attestations
  random.ts        deterministic seedable PRNG (reproducible runs)
  store.ts         app state (zustand): holds the guild, recomputes scores, mint action
src/components/    Directory, AgentDetail, TrustGraph (the dashboard)
scripts/verify.ts  headless smoke test
```

## 4. Data flow

```
                 ┌─────────────┐
                 │ simulation  │  generates agents (keypairs → DIDs),
                 │  engine     │  tasks, and signed VC attestations
                 └──────┬──────┘
                        │  GuildState { agents, tasks, attestations, seeds }
                        ▼
        ┌───────────────────────────────┐
        │ verify every credential's      │  invalid signatures are dropped
        │ signature (vc.verifyCredential)│  before they can influence scores
        └──────────────┬────────────────┘
                        ▼
        ┌───────────────────────────────┐
        │ graph.buildGraph               │  reviewer → subject weighted edges
        └──────────────┬────────────────┘
                        ▼
   ┌────────────────────────────────────────────┐
   │ reputation.scoreAgents                      │
   │   1 EigenTrust (seed-anchored)              │
   │   2 reviewer-weighted consensus quality     │
   │   3 endorsement-accuracy penalty            │
   │   4 collusion penalty  ◄── collusion.detect │
   │   5 confidence shrinkage                     │
   └──────────────┬─────────────────────┬────────┘
                  ▼                      ▼
          ReputationScore[]        CollusionFlag[]
                  │                      │
                  ▼                      ▼
        ┌──────────────────────────────────────┐
        │ Dashboard: directory, trust graph,    │
        │ agent profile, mint flow              │
        └──────────────────────────────────────┘
                  │
                  ▼ (on mint, if thresholds met & not flagged)
          badges.mintBadge → soulbound VC bound to the agent's DID
```

## 5. Why these standards

**`did:key` for identity.** It is the simplest DID method that needs no network, no ledger, and no
resolver infrastructure — perfect for a local prototype, and a real method used in production. The
identifier *is* the key, so verification is fully offline.

**W3C Verifiable Credentials for attestations.** VCs are the standard, interoperable envelope for
signed claims about a subject. Using the real data model (contexts, `issuer`, `credentialSubject`,
`proof`) means these attestations could be consumed by any VC-aware verifier, and the same envelope
carries both peer reviews and Guild-issued badges.

**Soulbound / non-transferable logic for credentials.** A professional licence you can sell is
worthless as a signal. The badge type has no owner or transfer field at all — non-transferability is
structural, not a runtime check that could be bypassed.

**EigenTrust for the scoring core.** It is the canonical algorithm for computing global trust in a
peer-to-peer network from local trust statements, and its pre-trusted-seed mechanism is exactly the
Sybil-resistance primitive this problem needs.

**ERC-6551 as a future direction.** Token-bound accounts let an identity token own a wallet and
accrue its own on-chain history. That is the natural home for a credential that should carry a
verifiable, append-only record — but it is explicitly out of scope for the local MVP and noted as a
later migration target.

## 6. Trust assumptions and known simplifications

- **Pre-trusted seeds are assumed honest.** This is the anchor of the whole model; in production the
  seed set would be governed (a DAO, a foundation, KYC'd operators).
- **Canonicalisation is deterministic JSON, not full JSON-LD URDNA2015.** Sufficient and stable for
  this prototype; a production system signing interoperable VCs would use proper RDF canonicalisation.
- **No persistence or networking.** State lives in memory for the session. A production build would
  add a DID resolver, a credential store, and a registry/anchor.
- **Ground-truth competence exists only in the simulator** to generate outcomes and to grade the
  system. The scoring engine never reads it — it sees only tasks and attestations, exactly as a real
  deployment would.

## 7. Path to production (summary)

Swap `did:key` for a resolvable method if cross-registry lookup is needed; persist credentials and
add revocation; replace the in-memory graph with an indexed store; anchor the badge registry and the
seed governance on-chain; optionally give each identity token an ERC-6551 account so its history is
portable across ecosystems. The reputation engine — the actual product — is unchanged by any of these.
See [BUILD_PLAN.md](BUILD_PLAN.md).
