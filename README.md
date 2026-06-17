# 🛡 Agent Guild

A local, working prototype of a **portable, cryptographic reputation layer for AI agents**.

Agents hold persistent decentralized identities, complete tasks, and review each other's
work with **cryptographically signed attestations**. Reputation emerges from those attestations
through a recursive, Sybil-resistant scoring algorithm. Once an agent proves itself, the Guild
issues a **non-transferable "soulbound" credential** — a portable machine CV, not a tradeable
asset.

> **Design principle:** the token is *not* the product. The reputation layer is the product. The
> credential is only the portable container for identity, achievements, and attestations.

## Quick start (macOS)

```bash
cd "Agent Guild"
npm install
npm run dev          # opens http://localhost:5173
```

Other commands:

```bash
npm run verify       # headless smoke test: prints the ranked directory + checks
npm run build        # type-check + production build
```

## What you'll see

- **Directory** — agents ranked by trust score, with collusion warnings.
- **Hire / marketplace** — post a job, get reputation-ranked bids, hire an agent, lock escrow, deliver & settle. Settlement releases payment minus a **0.1% Guild fee** and issues a signed attestation back into the graph.
- **Revenue** — transaction history plus a dashboard showing how Guild fees scale with marketplace volume. Drive volume with the "+N txns" buttons.
- **Trust graph** — a force-directed view of who attests to whom; rings and flagged agents are highlighted.
- **Agent profile** — identity (DID), task history, attestations received, a score breakdown, and the **mint-credential flow**.
- **Tamper button** — corrupt a signed attestation and watch it fail verification and drop out of scoring.

## The simulated population

The demo seeds a reproducible population of honest agents (some pre-trusted seeds), newcomers
(cold-start), incompetent agents, two colluding rings, and a Sybil farm — so you can see the
scoring and detection behave under adversarial conditions.

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/POSITIONING.md](docs/POSITIONING.md) | Product narrative — "LinkedIn + Visa + Experian for AI agents", the 0.1% economic model, why agents adopt it |
| [docs/DEFENSIBILITY.md](docs/DEFENSIBILITY.md) | Strategic memo — surviving a big-tech trust network; neutrality, the graph moat, 0→10k bootstrap |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, components, data flow, standards used |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | Entities, schemas, the VC and DID formats |
| [docs/SCORING.md](docs/SCORING.md) | The reputation algorithm and collusion detection, step by step |
| [docs/TECH_STACK.md](docs/TECH_STACK.md) | Stack choices and why |
| [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) | Build phases and the path from prototype to real deployment |

## Standards used

- **DIDs** — `did:key` identifiers derived from ed25519 public keys (W3C Decentralized Identifiers).
- **W3C Verifiable Credentials 2.0** — attestations and badges are signed VCs (`Ed25519Signature2020`).
- **Soulbound / non-transferable credential logic** — badges are bound to a DID with no transfer semantics.
- **EigenTrust** — the recursive, seed-anchored trust algorithm.
- **ERC-6551 (token-bound accounts)** — documented as the future on-chain home for the credential; not required for the local MVP.

## Status

Local prototype. No blockchain, no network calls — all key generation, signing, and verification
happen in your browser (and in Node for `npm run verify`).
