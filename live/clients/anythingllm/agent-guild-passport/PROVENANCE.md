# Source and licensing

Original AnythingLLM adapter and consumption policy: **AgentTanuki**, Apache-2.0.
The complete license is retained as `LICENSE.txt` and the unchanged upstream
[attribution notice](NOTICE.txt) as `NOTICE.txt`, extensions the inspected Hub
CLI includes. No AnythingLLM runtime implementation is bundled in this skill.

`verifier.js` is a deterministic CommonJS adaptation of
[Agent Guild's standalone verifier](https://github.com/AgentTanuki/agent-guild/blob/9cf6c561468e60afb77acdbddebfc5134a155e4f/sdk/agentguild_verify.mjs)
(SHA-256 `a61b7d64d79a076e48953fb8ade4b693fc7c082349d59a61a0d0e5f8f943a305`).
The extraction keeps `b58decode`, `publicKeyFromDid`, `edKey`, `canon`,
`multibaseB58Decode`, `verifyDataIntegrity` and the crypto import indirection.
Their bodies are unchanged; ESM imports/exports become CommonJS and unused
network, legacy, evidence, checkpoint and recommendation paths are omitted.
The original SDK's crypto import, hashes, canonicalization and Ed25519
verification remain the verification algorithm. New input/time/binding checks
are separate in `passport-policy.js`.

Regenerate from a checkout of that exact Guild commit:

```sh
node live/clients/anythingllm/tools/build-verifier.mjs
```

The script rejects a different SDK hash, reads only the supplied local source
and writes only the derived file. It performs no dependency install or network
request. The copied Apache license has SHA-256
`fd804fedd121e2ff1da46fb39da94e0ff3662aca96579dbb62372903c1eeedba`.

Contract references used for this version:

- [Guild STANDARD](https://github.com/AgentTanuki/agent-guild/blob/9cf6c561468e60afb77acdbddebfc5134a155e4f/docs/STANDARD.md),
  [actual VC emitter](https://github.com/AgentTanuki/agent-guild/blob/9cf6c561468e60afb77acdbddebfc5134a155e4f/live/guild/app/vc.py),
  and [store passport construction](https://github.com/AgentTanuki/agent-guild/blob/9cf6c561468e60afb77acdbddebfc5134a155e4f/live/guild/app/store.py).
- [AnythingLLM loader](https://github.com/Mintplex-Labs/anything-llm/blob/3a85d3e75490f09453de7e8c440b9463f4a82019/server/utils/agents/imported.js)
  and [manifest schema](https://github.com/Mintplex-Labs/anything-llm/blob/3a85d3e75490f09453de7e8c440b9463f4a82019/server/utils/agents/imported-manifest.schema.json).
- [Handler reference](https://github.com/Mintplex-Labs/anythingllm-docs/blob/60d94ffbd4d6b6ed8d8da2f1788fb65f565bdee3/pages/agent/custom/handler-js.mdx)
  and [custom skill guide](https://github.com/Mintplex-Labs/anythingllm-docs/blob/60d94ffbd4d6b6ed8d8da2f1788fb65f565bdee3/pages/agent/custom/developer-guide.mdx).
- [Hub CLI validator and bundle filter](https://github.com/Mintplex-Labs/anythingllm-hub-cli/blob/8bf7abc7cc99e99bd0eebcae2fa6be311969ad1b/utils/validateAgentSkill.js).
  It includes flat `.js`, `.json`, `.txt`, `.md` files and excludes `.mjs` and
  ordinary subdirectories. This is why the verifier is a sibling `.js` file.

Local test fixtures use a disclosed synthetic test key and the real pinned
Python emitter. They are not production Guild credentials, live customer
identities or evidence of adoption. Source references describe a reviewed
snapshot, not a promise of compatibility with future releases.
