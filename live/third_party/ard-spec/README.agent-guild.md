# Vendored ARD conformance assets

These files are copied verbatim from `ards-project/ard-spec` commit
`5fa2f5aef790b478319f6a3b43adf4661b0ed0e0` under its Apache-2.0 license:

- `conformance/bin/conformance-test` — SHA-256
  `fa387310d5f28358012ecb676b8257ef41e6015ea29905879e6802e0cb7df6b4`
- `spec/schemas/ai-catalog.schema.json` — SHA-256
  `c55238483a4738e08b250bdd6af1f4dc05a91afe882c649d224d09c19cd8fe09`
- `LICENSE` — SHA-256
  `dfe0e2a538e0e9004d43d1f57598177793109f5662706ccd1b1cb93c7fa34ce5`

They are pinned so CI and the post-deploy gate run the reviewed official tool,
not whatever a mutable upstream branch serves later.
