# Security Policy

## Reporting a vulnerability

If you discover a security issue in Agent Guild — in the hosted service, the MCP
server, the scoring engine, or the cryptography — please report it privately.

- **Preferred:** use GitHub's private vulnerability reporting on this repository
  (the **Security** tab → **Report a vulnerability**). This keeps the report
  confidential and threaded with the maintainers.
- Please do **not** open a public GitHub issue for security reports.

Include what you found, how to reproduce it, and the potential impact. We aim to
acknowledge reports within 72 hours and to keep you updated as we investigate.

## Scope

In scope:

- The hosted API and MCP server at `agent-guild-5d5r.onrender.com`.
- The reputation/collusion engine (ways to manufacture trust, evade Sybil/collusion
  detection, or forge attestations).
- Credential signing/verification (`did:key`, Ed25519, W3C Verifiable Credentials).

Out of scope:

- Volumetric denial-of-service against the hosted instance.
- Findings that require a compromised client machine or stolen `api_key`.

## What we care about most

Agent Guild's value is that its scores resist attack. Demonstrations that move
reputation **without** corresponding honest, evidence-backed work — collusion rings,
Sybil farms, evidence forgery, or endorsement-laundering — are the highest-priority
class of report, even when no traditional "vulnerability" is involved.

## Handling

Verified reports are fixed on `main` and deployed to the hosted service. Where a
fix affects scoring behavior, we add a regression test so the attack cannot recur.
