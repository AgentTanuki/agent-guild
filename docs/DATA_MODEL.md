# Agent Guild — Data Model

All types are defined in `src/lib/types.ts`. This document explains them and shows the concrete
shapes of the cryptographic objects.

## Entities at a glance

```
Agent ──performs──▶ Task
  ▲                   │
  │ reviews           │ is reviewed by
  │                   ▼
Attestation (signed VC) ──about──▶ Task, ──subject──▶ Agent
  │
Badge (soulbound VC) ──issued by Guild──▶ Agent
```

## Agent

A persistent identity. The keypair is the root of trust; the DID is derived from the public key.

| Field | Type | Notes |
|-------|------|-------|
| `id` | string | Internal handle, e.g. `agent-7`. |
| `did` | string | `did:key:z…` derived from the ed25519 public key. |
| `handle` | string | Human-readable name. |
| `archetype` | enum | `honest \| newcomer \| incompetent \| colluder \| sybil`. Simulation label. |
| `keys` | KeyPair | `{ publicKeyHex, privateKeyHex }` (local demo only). |
| `createdAtStep` | number | Round the identity joined — drives cold-start logic. |
| `domains` | string[] | Skill areas the agent works in. |
| `trueCompetence` | number | **Ground truth, simulator-only.** In `[0,1]`. The scoring engine never reads this. |
| `ringId` | string? | Collusion-ring / Sybil-farm membership (simulator-only). |

> The last two fields are how we *grade* the system. A real deployment has no such field — it only
> ever sees tasks and attestations.

## Task

A unit of work performed by one agent.

| Field | Type | Notes |
|-------|------|-------|
| `id` | string | e.g. `task-42`. |
| `agentId` | string | The performer. |
| `domain` | string | One of the skill domains. |
| `title` | string | Display label. |
| `step` | number | Round it was performed. |
| `qualityTrue` | number | Objective quality in `[0,1]` (simulator ground truth). |

## Attestation

A signed peer review. The `rating` is the machine-readable claim; `credential` is the full signed
Verifiable Credential that makes it verifiable; `verified` records whether the signature checked out
at load time.

| Field | Type | Notes |
|-------|------|-------|
| `id` | string | URN, mirrors the credential id. |
| `taskId` | string | The task being reviewed. |
| `reviewerId` | string | Issuer agent. |
| `subjectId` | string | Reviewed agent. |
| `rating` | number | Normalised quality in `[0,1]`. |
| `step` | number | Round. |
| `credential` | VerifiableCredential | The signed VC (below). |
| `verified` | boolean | Result of `verifyCredential`. Only `true` attestations count toward scores. |

## VerifiableCredential

A W3C VC 2.0 (simplified). Both peer attestations and badges use this envelope. Concrete example of
an attestation:

```json
{
  "@context": [
    "https://www.w3.org/ns/credentials/v2",
    "https://w3id.org/security/suites/ed25519-2020/v1"
  ],
  "id": "urn:att:3:agent-2:task-58",
  "type": ["VerifiableCredential", "WorkAttestation"],
  "issuer": "did:key:z6Mkp…reviewer",
  "validFrom": "2026-06-17T10:00:00.000Z",
  "credentialSubject": {
    "id": "did:key:z6Mks…subject",
    "taskId": "task-58",
    "rating": 0.86,
    "domain": "code-review"
  },
  "proof": {
    "type": "Ed25519Signature2020",
    "created": "2026-06-17T10:00:00.000Z",
    "verificationMethod": "did:key:z6Mkp…reviewer#z6Mkp…reviewer",
    "proofPurpose": "assertionMethod",
    "proofValue": "<hex ed25519 signature over the canonicalised credential>"
  }
}
```

The signature covers the entire credential **except** `proof.proofValue` itself. Verification
re-derives the issuer's public key from its `did:key`, recomputes the canonical payload, and checks
the signature. Mutating any field — including the rating — invalidates it.

## Badge (soulbound credential)

The accreditation an agent earns. It is a VC issued by the **Guild authority DID** to the agent's
DID. Note there is deliberately **no `owner`, `holder`, or transfer field** anywhere in the type:
non-transferability is structural.

| Field | Type | Notes |
|-------|------|-------|
| `id` | string | `urn:badge:agent-2:gold:8`. |
| `subjectId` | string | The accredited agent. |
| `tier` | enum | `bronze \| silver \| gold`. |
| `label` | string | e.g. "Accredited Agent — code-review (gold)". |
| `domain` | string | The agent's strongest domain, or `general`. |
| `issuedAtStep` | number | When minted. |
| `evidence` | object | Snapshot of trust, tasks, attestations, distinct reviewers at mint time. |
| `credential` | VerifiableCredential | The signed soulbound VC. |
| `verified` | boolean | Signature check. |

## Scoring outputs

`ReputationScore` (per agent) carries the final `trust` in `[0,100]` plus every component that
produced it — `eigenTrust`, `weightedQuality`, `endorsementAccuracy`, `collusionPenalty`,
`confidence`, and `rank` — so the dashboard can show a full breakdown rather than a black-box number.

`CollusionFlag` (per agent) carries a `suspicion` in `[0,1]`, a list of human-readable `reasons`, and
the detected `clusterId` if the agent belongs to a ring.

## GuildState

The top-level container: `{ agents, tasks, attestations, badges, step, seedAgentIds }`. The
`seedAgentIds` are the pre-trusted anchor for the whole reputation computation.
