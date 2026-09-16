# AnythingLLM: optional offline Agent Passport skill

[The self-contained skill](agent-guild-passport/README.md) verifies a supplied
public passport against an administrator-pinned issuer and an independently
known subject DID. It performs no external calls and makes no hiring or
execution decision. Author: **AgentTanuki**; Apache-2.0.

Only the flat `agent-guild-passport/` directory is the distributable skill.
This parent `package.json` provides CommonJS scope inside Guild's ESM repository;
it is not a runtime dependency or part of the Hub upload. No package install is
required. The host's own model and logging configuration still applies to tool
arguments; see the skill's privacy limits.

From this directory, run the meaningful offline handler tests:

```sh
node --test tests/handler.test.cjs
```

The checked-in fixture was issued by the actual pinned Guild Python emitter
using a public synthetic test seed `000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f`.
Its test clock is fixed at `2026-09-13T06:00:00Z`; it is not a current production
credential. Tests cover exact binding, signature tampering, signed time policy,
JSON limits and the absence of skill network/logging calls. Mutated signed
fixtures use this test key only.

The deterministic [adaptation script](tools/build-verifier.mjs) regenerates only
the verification subset from the pinned local Guild SDK. Source provenance and
native compatibility limits are in [the skill](agent-guild-passport/PROVENANCE.md).
There is no build bundle, service deployment or runtime installation step.
