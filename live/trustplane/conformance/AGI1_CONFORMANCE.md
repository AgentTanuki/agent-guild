# AGI-1 Conformance — trust evidence any issuer can issue, any verifier can check

AGI-1 is the interoperable trust-evidence standard the Agent Guild trust
plane rides on. It is deliberately small: W3C Verifiable Credentials with
`DataIntegrityProof` / `eddsa-jcs-2022` over `did:key` (Ed25519), an
append-only checkpoint feed, and the AGD-1 decision contract. Nothing in it
requires the Agent Guild: any registry can be an issuer, any agent or
framework can be a verifier.

## Issuer requirements (I-1 … I-7)

* **I-1 identity** — the issuer is a `did:key` (Ed25519). Key rotation is an
  append-only, signed ledger entry chaining old→new DID (verifiers check
  continuity from a pinned root).
* **I-2 proofs** — every issued credential/decision carries a conforming
  `DataIntegrityProof`, cryptosuite `eddsa-jcs-2022` (JCS canonicalisation,
  `hashData = SHA256(JCS(proofConfig)) || SHA256(JCS(doc))`, raw Ed25519,
  base58btc-multibase `proofValue`, did:key `verificationMethod`).
* **I-3 validity windows** — every signed document carries `issued_at`/
  `valid_until` (or VC `validFrom`/`validUntil`). Unbounded assertions are
  non-conformant.
* **I-4 decisions** — delegation decisions conform to AGD-1: identity,
  capability match, estimate, confidence, staleness, reachability,
  value-at-risk support, evidence provenance, and a CALLER-owned policy slot.
  Issuers MUST NOT make the hire/no-hire decision for callers.
* **I-5 checkpoint feed** — the issuer publishes an append-only checkpoint
  feed; each entry commits to its predecessor (`prev_entry_sha256`) AND is
  ISSUER-SIGNED at the entry level (`entry_proof`, feed_version ≥ 2), so a
  verifier checks feed SIGNATURES and continuity, not only hashes (I-5b).
  Legacy entries that predate entry signatures are acceptable ONLY when a
  later signed entry carries a versioned `bridge` committing to their exact
  bytes — immutable history is never rewritten.
* **I-6 one-counterparty binding** — when a decision's routing gate is
  routable, the decision it serves for that route MUST concern the SAME
  provider: `decision.agent_id == routing.provider_id`, matching DID, matching
  endpoint + `endpoint_sha256`, and the same requested capability. An issuer
  that cannot establish this MUST fail closed (serve `routable=false`), never
  a decision about one identity attached to a route to another. A separate
  non-actionable `highest_ranked` object MAY be exposed but must never be
  confused with the routed/evaluated provider.
* **I-7 evidence-to-checkpoint inclusion** — a decision counts only evidence
  its cited checkpoint actually COMMITS. The issuer exposes, for every counted
  record, a verifiable Merkle inclusion proof to the cited checkpoint's root
  (`GET /ledger/inclusion/{record_id}`); records newer than the checkpoint are
  excluded from counts and value-tier support. Provenance labelling is
  explicit (`guild_mediated` > `verifiable_outcome` > `mutual_attestation` >
  `external_import` > `one_party_claim`/`first_party_bootstrap`), and no
  record reaches the top class on one party's word.

## Verifier requirements (V-1 … V-5)

* **V-1** — verify proofs offline with an independent implementation of I-2
  (no issuer SDK required; two exist in this repo: `verifiers/` and
  `agentguild_trustplane/verify.py`).
* **V-2** — reject tampered, expired, or unbounded documents.
* **V-3 issuer trust policy** — maintain an explicit issuer allowlist (or
  pinned TOFU set). Credentials from unknown issuers are UNVERIFIED, never
  silently accepted. Multiple issuers are supported; each is checked against
  its own DID only.
* **V-4 fork detection** — pin checkpoints. Two checkpoints from one issuer
  claiming the same index with different `head_hash` (or a broken
  `prev_entry_sha256` chain) is a FORK: flag the issuer, stop trusting new
  credentials until resolved.
* **V-5 caller-owned thresholds** — a verifier that turns AGI-1 evidence into
  an automatic decision does so under ITS OWN policy (AGD-1 policy slot),
  never under a verdict shipped by the issuer.

Verifiers additionally reject a CHANGED issuer unless a verified dual-signed
rotation chain (`GET /ledger/rotations`) connects it to a pinned issuer, and
persist their TOFU pins across restart (V-3).

## Running the suite

    pytest conformance/                                          # local issuer, booted for you
    pytest conformance/ --issuer-base=<https://your-issuer>      # any live issuer
    pytest conformance/ --issuer-base=https://agent-guild-5d5r.onrender.com --capability=hello

The suite is issuer-agnostic: point it at any implementation of I-1…I-7. With
no `--issuer-base` it boots the production Agent Guild app locally, seeds
evidence, and runs against it, so `pytest conformance/` works offline out of
the box. `suite.py` is dependency-light (cryptography only) and vendorable;
CI runs it against both the local issuer and the live service.
