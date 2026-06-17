# Agent Guild — Tech Stack

## Summary

A single TypeScript + React app, built with Vite, that runs entirely in the browser. No backend, no
database, no blockchain. One `npm install && npm run dev`.

## Choices and rationale

**TypeScript + React + Vite.** The hardest part of the brief is the *dashboard* — a ranked directory,
an interactive trust graph, drill-down profiles, and a live mint flow. That is a UI problem, and
React with a fast dev server is the most direct path. TypeScript keeps the domain model (credentials,
scores, graph) honest across the whole codebase. A single-app design means there is no API contract to
maintain and nothing to deploy to run it locally.

**`@noble/ed25519` + `@noble/hashes`.** Audited, dependency-light, pure-JS ed25519. It runs identically
in the browser and in Node (used by the verify script), so the same signing and verification code
path is exercised everywhere. ed25519 is the signature scheme behind `Ed25519Signature2020` VCs and
the `did:key` ed25519 method, so this one primitive covers identity, attestations, and badges.

**`@scure/base`.** Provides the base58btc encoding used in `did:key`. Same author and security posture
as the noble libraries.

**`d3-force`.** A well-tested force-directed layout for the trust graph. We run the simulation to
convergence once per dataset and render plain SVG — no canvas, no heavy graph library.

**`zustand`.** Minimal global state. It holds the guild, recomputes scores when the data changes, and
exposes the mint action. Chosen over Redux/Context boilerplate because the state shape is small and the
recompute-on-change pattern is a one-liner.

## What we deliberately did **not** use

- **No blockchain / web3 libraries.** The brief asks for a local prototype, and the reputation layer —
  not the chain — is the product. Soulbound and ERC-6551 semantics are modelled in plain types and
  documented as a migration target.
- **No DID resolver / VC framework (Veramo, etc.).** Those bring network resolution and heavy
  dependency trees. `did:key` is self-resolving, so we implement the small, transparent slice of the
  VC and DID specs we actually need.
- **No backend / database.** Nothing in the MVP needs persistence beyond a session. Adding one is a
  clean later step (see BUILD_PLAN).
- **No CSS framework.** A single hand-written stylesheet keeps the bundle small and the styling legible.

## Footprint

About a dozen small source files, ~80 npm packages (mostly Vite/esbuild tooling), a ~70 KB gzipped
production bundle. Verified with `npm run build` (type-check + bundle) and `npm run verify` (headless
logic test).

## Requirements

Node 18+ and npm. Developed and verified on Node 22. macOS-friendly; nothing platform-specific.
