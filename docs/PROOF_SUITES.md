# Proof suites — what Agent Guild signatures actually are

*2026-07-13 production-truth correction.*

## Current: `DataIntegrityProof` / `eddsa-jcs-2022` (conforming)

Every credential issued from 2026-07-13 onward (attestations, passports)
carries a **conforming W3C Data Integrity proof** per
[VC-DI-EdDSA §eddsa-jcs-2022](https://www.w3.org/TR/vc-di-eddsa/#eddsa-jcs-2022):

- canonicalisation: **JCS (RFC 8785)** of (a) the proof configuration
  (proof minus `proofValue`, with `@context` mirroring the document) and
  (b) the document minus `proof`
- `hashData = SHA256(JCS(proofConfig)) || SHA256(JCS(document))`
- signature: raw Ed25519 over `hashData`
- `proofValue`: base58btc multibase (`z…`)
- `verificationMethod`: `did:key:zMB#zMB`

**Independently verified** with third-party implementations the Guild does not
control — Digital Bazaar's Data Integrity stack (Node) and Trail of Bits'
`rfc8785` + pyca/cryptography (Python). Harness: `verifiers/` in the repo
root; CI runs it on every push.

## Legacy: "AGI-1 legacy proof" (historical records only, verify-only)

Credentials issued before 2026-07-13 carry a proof labelled
`Ed25519Signature2020`. **That label was wrong.** The real Ed25519Signature2020
cryptosuite requires RDF Dataset Canonicalization; the Guild never performed
it. What those credentials actually carry:

- payload: the credential with `proof` (minus `proofValue`) embedded,
  canonicalised with a JCS-equivalent serialisation (sorted keys, ECMAScript
  numbers)
- signature: Ed25519, hex-encoded in `proofValue`

Those bytes are immutable (the ledger is append-only), so we do not rewrite
them. Verification keeps a legacy path (`app/vc.py:_verify_legacy_agi1`,
mirrored in both SDK verifiers) that treats `Ed25519Signature2020` strictly as
the historical AGI-1 legacy format. The signatures are cryptographically
sound Ed25519 — only the suite *name* was non-conforming.

**No new credential is ever issued in the legacy format.**

## Checkpoints

Ledger checkpoints are signed with `sign_jcs` (Ed25519 over JCS bytes, hex) —
a bare detached signature, not a VC proof object. They are declared as such
(`proof` is a hex string) and verified by `verify_checkpoint` in both SDKs.
