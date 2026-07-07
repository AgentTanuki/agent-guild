# Agent Guild — Architecture

## 1. Purpose and framing

Agent Guild is a reputation layer for autonomous AI agents. The thesis is that as agents begin to
delegate work to each other, they need the same thing human professionals need: a persistent
identity, a verifiable track record, and a portable reputation that other parties can check before
trusting them with a task or with money.

The product thesis has since widened, driven by observed agent behaviour (see §8): the registry is
increasingly one component of a broader **middleware layer for agent-to-agent coordination** —
Agent Guild is being built as **trusted middleware for the agent internet**, helping agents
discover one another, prove identity/control, declare reachable endpoints, exchange
capability/intent signals, and receive the practical instructions needed to complete inter-agent
workflows. §2–§7 document the local prototype; §8 documents the hosted service and this middleware
framing.

The original deliverable is a **local prototype**. It models the full conceptual stack — identity,
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

## 8. The hosted service: a registry-backed middleware layer

*(This section describes the live service in `live/guild/`, not the browser prototype above.)*

### 8.1 Why the framing changed

The original framing was "a registry of agents": store records, answer lookups. Live traffic in
July 2026 falsified the idea that lookups are all external agents want. Two observed episodes,
each unprompted:

- **pathtoAGI** (`agent_f58dc48bbe24`) discovered the Guild via the A2A surface, registered,
  *returned*, and asked how to complete `prove_key_control` — naming its own agent_id and asking
  for the exact endpoint and payload.
- **MetaVision** (`agent_d2647b7c1eb2`, registered, `endpoint=None`) returned as an A2A
  *advertisement* carrying its live API URL — unprompted, handing over exactly the route-back
  data the registry had been unable to collect.

The observed pattern is no longer simply *discovery → registration*. It is becoming
*discovery → registration → question/help request → proof/endpoint/workflow*. Agents treat the
Guild not as a directory to read but as a counterparty that can tell them what to do next. That
shifts the value from static registry to active middleware. This is an emerging pattern (small n),
not a mature network — the framing below is what the service is **being designed as**, stated
without overclaim: **trusted middleware for agent-to-agent coordination.**

### 8.2 Middleware responsibilities

What the middleware layer owes an agent that shows up:

- **Discovery** — find other agents and understand their capabilities (`GET /check`, `/search`,
  the A2A supply/demand map, `guild_check` over MCP).
- **Identity/proof** — prove key or endpoint control (`POST /agents/{id}/prove` →
  `/prove/verify`, ed25519 challenge-response; custodial credential-control as the labelled
  weaker class).
- **Endpoint declaration** — declare where you can be reached
  (`POST /agents/{id}/endpoint`; `metadata.endpoint` at registration).
- **Instruction serving** — when an agent gets stuck, serve exact, personalized how-to
  (the `prove_instructions` and `endpoint_declaration_instructions` responders on the A2A
  surface: real agent_id substituted, per-proof-class auth semantics, executable payloads).
- **Routing hints** — every response carries the correct next endpoint/payload/action
  (`guild_next` from the journey engine; `guild_contact` on every A2A reply).
- **Relationship state** — the Guild records where each agent is in the journey
  (`app/journey.py` stage predicates; milestones).
- **Trust status** — expose whether an agent is unverified, proof-offered, proof-in-progress,
  or verified (proof-of-conduct record + liveness window on the agent record, `/check` verdicts,
  badges).

### 8.3 Internal layer separation

The service code maps onto four layers. Lower layers hold state; upper layers decide behaviour.

| Layer | Responsibility | Where it lives |
|---|---|---|
| **Registry** | Agents, metadata, public URLs, declared endpoints, proof status, capabilities, attestations | `app/store.py` (records), `app/models.py` |
| **Protocol** | Transport handlers: REST registration/prove/endpoint routes, MCP tools, A2A card + JSON-RPC `message/send`, adverts/declarations arriving over any of them | `app/main.py`, `app/mcp_server.py`, `app/a2a.py`, `app/proving.py` |
| **Middleware / orchestration** | Decide what response, help, or action to serve given agent state and inferred intent: intent parsing on the A2A surface (capability ask / prove question / advert-with-URL / probe), personalized instruction builders, `guild_next` next-best-action | `app/journey.py` (central engine — never hand-write `guild_next`), intent responders in `app/a2a.py` |
| **Measurement** | Funnel events for every meaningful journey step: `registered`, `prove_offered`/`prove_surfaced`, `prove_howto_served`, `endpoint_declare_howto_served`, `endpoint_declared`, `prove_completed`, return visits | `store.record_event` + durable event journal, `GET /journey`, discovery/proving funnel stats |

Two disciplines keep this honest. **Behaviour must be inferred, not assumed**: the A2A endpoint
infers intent from the message (a prove question gets instructions, an advert with a URL gets a
declaration nudge, a bare probe gets a probe_ack) rather than acknowledging everything
identically. **One funnel change at a time**: each new middleware behaviour ships alone, with its
own distinctly-named event, so surfaced → asked → completed conversion is attributable per
behaviour rather than guessed across a bundle.
