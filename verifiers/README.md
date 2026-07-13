# Independent verification harness

Proves that Agent Guild credentials carry a **conforming W3C Data Integrity
proof** (`DataIntegrityProof`, cryptosuite `eddsa-jcs-2022`,
https://www.w3.org/TR/vc-di-eddsa/#eddsa-jcs-2022) — verified by code the
Guild does not control:

* `verify_node_digitalbazaar.mjs` — Digital Bazaar's `jsonld-signatures` +
  `@digitalbazaar/data-integrity` + `@digitalbazaar/eddsa-jcs-2022-cryptosuite`.
* `verify_python_independent.py` — Trail of Bits' `rfc8785` (JCS) + `base58` +
  `pyca/cryptography`, with the spec's verification algorithm transcribed
  directly (no Guild code imported).

Both also check that a tampered credential is REJECTED.

```bash
# 1. generate a vector from the issuer code
cd live/guild && python ../../verifiers/gen_vector.py ../../verifiers/vector.json && cd ../..
# 2. Node / Digital Bazaar
cd verifiers && npm install && node verify_node_digitalbazaar.mjs vector.json
# 3. Python / rfc8785
pip install rfc8785 base58 cryptography && python verify_python_independent.py vector.json
```

Historical credentials issued before 2026-07-13 carry the **AGI-1 legacy
proof** (mislabeled `Ed25519Signature2020`) — see `docs/PROOF_SUITES.md`.
They remain verifiable via the SDK verifiers but are NOT W3C-conforming;
no new credential is issued in that format.
