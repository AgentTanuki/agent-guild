# Corrections — 2026-07-17 (machine-integrity pass on the attribution layer)

Corrective commit on top of the 2026-07-15/16 machine-attribution work
(81f2fa4 + queued commits). Regression tests were written FIRST; every item
below is pinned by a test named here.

## P0 — honest economic semantics (`verified_external_machine` retired)

**Defect.** A self-created did:key plus a self-controlled wallet proves
machine identity continuity and wallet control. It does NOT prove the payer
is external to Agent Guild — an AG-controlled process can mint both. The
`verified_external_machine` class claimed externality from exactly that
evidence.

**Correction** (`app/payments.py::ATTRIBUTION_CLASSES`, `app/externality.py`,
`app/store.py` funnel + `/billing/revenue`):

* `verified_first_party_canary` — unchanged; never external revenue.
* `cryptographically_bound_machine_payer` — valid caller proof + exact
  `(address, network)` wallet binding; ownership/externality UNPROVEN;
  legacy `first_party_payer` stays `None` (unknown), never `False`.
* `independently_attested_external_machine` — ONLY with a currently-valid
  externality attestation from a SEPARATE allowlisted issuer
  (`GUILD_EXTERNALITY_ATTESTOR_DIDS`, default EMPTY ⇒ totals honestly zero;
  self- and Guild-issued attestations never count, even if allowlisted).
* `unverified_payer` — missing proof is UNKNOWN, never external.

Revenue exposes `cryptographically_bound_machine_revenue_usd` and
`independently_attested_external_revenue_usd` separately;
`verified_external_revenue_usd` is gone. Historical records labelled
`verified_external_machine` are re-interpreted at READ time as
cryptographically bound (their evidence only ever proved binding) — history
is never rewritten. Funnel stages renamed accordingly
(`cryptographically_bound_machine_settlement`,
`independently_attested_external_settlement`).

**Decisive negative test**
(`tests/test_attribution_honesty.py::test_self_created_did_and_wallet_never_becomes_attested_external`):
an AG-controlled process creates a fresh DID + wallet, completes the real
dual-signature binding flow and pays — it becomes cryptographically bound
and NEVER enters independently attested external revenue.

## P0 — MCP caller-proof actually wired

**Defect.** The MCP `_meta["io.agent-guild/caller-proof"]` mapping was
documented but `mcp_server.py` never read or verified it.

**Correction** (`app/mcp_server.py::CallerProofMiddleware`): one
verification per `tools/call`, bound to method `tools/call`, the exact tool
name, and `sha256(JCS(visible arguments minus api_key/_meta))`. The
(verified, DID) outcome rides a request-scoped contextvar into BOTH
`demand.record_demand` (actor `did:<did>`, `caller_proof_verified`) and
`payments.authorize` (settlement attribution). The nonce is consumed once —
never verified twice within one call
(`tests/test_mcp_caller_proof.py::test_one_proof_serves_demand_and_settlement_attribution`
fails if it is). Native FastMCP execution-path tests cover `guild_check`,
`guild_search`, `guild_best_agent`, `guild_risk_score`, tampering, replay,
absence, and idempotent-replay attribution.

## P0 — clean-install truth

* `eth-account>=0.13,<0.14` pinned in `live/guild/requirements.txt` (the
  runtime imported it; the manifest omitted it).
* Python verifier manifest `verifiers/requirements.txt` (rfc8785, base58,
  cryptography, eth-account); Node deps (canonicalize, bs58, js-sha256,
  @noble/ed25519, ethers) pinned in `verifiers/package.json` + lockfile.
* CI job `caller-proof-wallet-verification`: generates the vector from the
  live issuer code, then verifies in a CLEAN venv (pinned manifest) and a
  CLEAN `npm install` — never the repo's runtime environment.
* The wallet verifiers now verify the ACTUAL Guild-issued credential
  (issuer signature, issuer DID, validity window, subject fields), not
  merely the two pre-issuance binding signatures. Offline cryptographic
  validity and live revocation/status are SEPARATE
  (`walletbinding.credential_offline_valid` vs `credential_status_live`);
  no surface describes a live-store lookup as offline verification.

## P0 — wake rate limiting made real

**Defect.** `notify_demand()` computed a debounced `dispatch` decision and
then called `_kick.set()` unconditionally; the loop re-ran immediately
whenever the queue was non-empty. The debounce was decoration.

**Correction** (`app/swarm/runner.py`): persisted, deadline-aware
scheduler. Recording into the durable queue is never debounced; a
demand-triggered run must CLAIM the persisted dispatch slot at run start
(`_claim_dispatch`, ≤1 per `GUILD_SCOUT_WAKE_DEBOUNCE_S`, enforced under
the lease); the loop plans against `next_dispatch_at` (persisted; survives
restart) so pending work runs when the window expires — not six hours
later; queued capabilities survive delay/failure/lease collision/restart;
the interval remains a fallback. Fake-clock + concurrency tests:
`tests/test_wake_scheduler.py` (flood of actors × capabilities → bounded
dispatches, eventual drainage).

## P1

* Binding hardening (`app/walletbinding.py`): `binding.expires_at` must
  equal the stored challenge expiry exactly; DID validated before a
  challenge persists; CAIP-2 settlement-network allowlist; expired-challenge
  GC + per-DID (8) + global (10k) bounds; deterministic supersession — one
  `(address, network)` resolves to exactly one active DID.
* Wallet lookup is exact `(address, network)`; the SETTLED network feeds
  attribution (`store.active_wallet_binding`).
* Caller proofs require `iat < exp` (positive bounded lifetime).
* Attribution fields survive into cached idempotent settlement records and
  native MCP replay responses.
* Demand semantics: a valid caller proof establishes verified MACHINE
  demand regardless of User-Agent (curl/empty/python-requests tested);
  invalid proofs never do; known first-party DIDs stay excluded; verified
  machine identity is never conflated with verified external ownership.
* Version 2.0.0 → 2.0.1; contracts regenerated (`contract.json`,
  `server.json`, `docs/INTERFACE.md`); registry publish + readback trigger
  on the `server.json` change at the eventual push.

## Honesty notes

* Real external revenue remains $0; nothing in this pass manufactures
  demand, revenue, settlement or outbound contact.
* `independently_attested_external_machine` is implemented but its total is
  honestly ZERO until a real independent attestor exists and is explicitly
  allowlisted.
