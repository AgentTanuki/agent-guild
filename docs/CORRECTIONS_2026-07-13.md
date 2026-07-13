# Corrective integrity pass — 2026-07-13

This pass made Agent Guild safe for machines to consult and enforce
**automatically, with no human in the operational loop**. It fixed underlying
invariants and added regression tests; it added no new capabilities, marketing
claims, or evidence narratives. Where prior documentation or evidence
contradicts the corrected behaviour, this file is authoritative and the older
artifacts carry a `corrective_addendum` pointer back here.

## What was wrong, and what is now true

### One counterparty for decision, route, invocation and outcome (P0)
The live `GET /check?capability=hello&signed=true` returned
`decision.agent_id = Forge-9` while `routing.provider_id = Hello World Agent` —
a machine could approve one identity and invoke another. `/check` now binds the
**evaluated** provider to the **routed** provider: when `routing.routable` is
true, `decision.agent_id == routing.provider_id`, the DID matches, and the
endpoint + `endpoint_sha256` match. Any mismatch **fails closed**
(`routable=false`). The evidence-ranked #1, when it is not the routed provider,
is exposed only as a non-actionable `highest_ranked` object. GateResults are
bound to the signed-envelope hash, provider id, provider DID, endpoint
fingerprint, capability, policy id and value tier; outcomes cite that binding
and can never be credited to another provider.

### Verify live evidence before using it (P0)
`GuildClient.signed_decision()` returned the live document even when the cache
rejected its proof or issuer. Every live decision is now cryptographically
verified, issuer-pinned, inside its validity window, AGD-1-conformant and
counterparty-bound before use. A verification failure is an `unverified`
state, never `live`, and enforce-mode policy fails closed on it (it is an
integrity signal, not an availability one). TOFU issuer pins persist across
restart; a changed issuer is rejected unless a verified dual-signed rotation
chain (`GET /ledger/rotations`) connects it to a pinned issuer.

### Bind every integration to the actual destination (P0)
Framework wrappers now either invoke the endpoint the signed route selected
(read via `gateway.current_gate().endpoint`, a context-scoped value that
survives framework executors) or accept an explicit destination that is
verified to **exactly** match the signed decision (`bind_destination`). The
A2A sidecar rejects an explicit endpoint unless it matches the signed route +
fingerprint; `/report` rejects unknown gate ids and caller-supplied identity
changes. The MCP proxy will not use one provider's reputation to authorize an
unrelated downstream server: it requires the downstream's verified Guild
identity binding, or applies the caller's unknown-counterparty policy.

### Automatic settlement under production hashed credentials (P0)
Production runs `GUILD_HASH_KEYS=1`, so the store holds public key ids, not
secrets. Market timeouts and dispute execution previously replayed a stored
key id into methods that require the raw secret; every automatic settlement
failed and the error was swallowed while disputes were still marked resolved.
Settlement now uses **internal** operations authorized by immutable escrow
ownership/state, never a replayed credential. Public HTTP methods still
require the raw requester credential. Failures are journalled and retried
idempotently (across restart); a dispute is marked resolved only when funds
reach a terminal state; appeals reverse the round-1 settlement (clawing back a
spent payout fails the appeal rather than paying twice). Funds always end in
exactly one terminal state.

### Substantive checkpoint anchoring (P1)
A decision now counts only evidence its cited checkpoint actually commits
(records newer than the checkpoint are excluded and disclosed), and exposes a
verifiable Merkle inclusion proof per counted record
(`GET /ledger/inclusion/{record_id}`). The checkpoint feed is entry-signed
(`entry_proof`, feed_version 2) with predecessor commitments; legacy entries
are covered by a signed, versioned bridge rather than a silent rewrite. The
conformance suite proves evidence-to-checkpoint inclusion (I-7).

### Real outcome completion (P1)
Outcomes are a server-side signed contract (AGO-1): the requester signs with a
DID it controls, the outcome is bound to the gate-envelope hash, provider DID,
task ref and deliverable hash, and it is sealed on the ledger
(`POST /outcomes`). `evidence_recorded` is counted only after the sealed
record is **read back** and verified (`GET /ledger/record/{id}`). The A/B
experiment fails (`status: FAIL`) rather than claims completion if any outcome
flush is unresolved.

### Attribution and economic reporting (P1)
`/billing/revenue` reports honest classes — first-party sandbox, third-party
unconsented, consenting-external sandbox, and real settlement — with the
explicit statement that **sandbox credits are not money and actual revenue is
zero** until an independently verifiable on-chain/fiat settlement exists. x402
is "ready but inactive" (no funded treasury). All Agent Guild-operated scripts
route through a shared first-party header helper that is never empty (the old
per-script copies returned `{}` without a token and silently counted as
external). `live/scripts/reclassify_guild_operated.py` flags any
already-registered Guild-operated identities first-party without rewriting
immutable transaction history.

### A/B evidence rebuilt honestly (P1)
The harness now runs the **same unmodified** `delegate` tool in both arms; the
gated arm wraps it in each framework's **real** interceptor (CrewAI, LangChain,
LangGraph, OpenAI Agents, the MCP proxy Server, the sidecar `/a2a/forward`).
Body-entry counters prove the tool body cannot run on a denied gate; every
gated invocation is asserted to hit the signed route; every outcome is a
signed ledger record verified by readback; the queue ends empty or the
artifact is `status: FAIL`. The artifact is explicitly labelled a **local
first-party warm-up on production code over loopback — not production traffic
and not external evidence**. The random baseline is described as such.

### Install / CI / registry (P1)
The combined pin set did not resolve (CrewAI 1.15.2 needs `mcp~=1.26` while the
proxy pinned `mcp==1.28.1`). Framework extras are now independently resolvable
environments under `live/trustplane/requirements/` with one reproducible clean
command each (`make trustplane-install-<name>`). CI gained trust-plane jobs:
core suite + local conformance, a per-extra clean-install-resolution matrix
with native adapter tests, and live-service conformance. `server.json` now
validates (`mcp-publisher validate` passes: lowercase namespace, ≤100-char
description) and the publish workflow gates on a registry **readback** that
confirms the published version and trust `_meta` before treating publication
as done.

## Honest status of external claims

There is **no real revenue and no external settlement**. The two prior
"genuine external transaction" narratives describe Guild-observed invocations
of a public endpoint (third-party, unconsented, sandbox) — not consenting
external economic transactions and not real payments. Sandbox credits are a
bookkeeping unit, never USD.

## Release gates

See the commit message / run report for the pass/fail of each of the 13
release gates. Gates requiring live infrastructure (registry publication
readback, live signed-decision verification against the deployed service) are
satisfied by CI on push; they cannot be fully closed from a local working tree
and are reported honestly as such rather than asserted.
