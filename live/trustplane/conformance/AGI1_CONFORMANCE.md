# AGI-1 Conformance — trust evidence any issuer can issue, any verifier can check

AGI-1 is the interoperable trust-evidence standard the Agent Guild trust
plane rides on. It is deliberately small: W3C Verifiable Credentials with
`DataIntegrityProof` / `eddsa-jcs-2022` over `did:key` (Ed25519), an
append-only checkpoint feed, and the AGD-1 decision contract. Nothing in it
requires the Agent Guild: any registry can be an issuer, any agent or
framework can be a verifier.

## Issuer requirements (I-1 … I-6)

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
  feed; each entry commits to its predecessor (`prev_entry_sha256`) and is
  issuer-signed. Credentials cite the latest published checkpoint.
* **I-6 provenance labelling** — evidence classes are explicit
  (`guild_mediated` > `verifiable_outcome` > `mutual_attestation` >
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

## Running the suite

    pytest conformance/ --issuer-base=<https://your-issuer>      # live issuer
    pytest conformance/                                          # bundled vectors

The suite is issuer-agnostic: point it at any implementation of I-1…I-6.
`suite.py` is dependency-light (cryptography only) and vendorable.
