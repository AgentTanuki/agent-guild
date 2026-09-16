# Agent Guild plugin for elizaOS

Two explicit actions let an agent observe a newly selected public endpoint or verify a supplied public Agent Guild passport. They provide evidence for a decision; they do not intercept other actions or authorize delegation, payment or installation.

This is an **unpublished external plugin draft**. Its action and plugin shapes follow `elizaOS/eliza` commit `a5e06fb49c30a09f07504472c7b987dfd1775823`, whose core package declares `2.0.3-beta.7`. The package was unavailable to this validation environment, so native loading, native type compatibility and live-model execution remain pending. Do not present the source review or the offline tests as those checks. Package-name availability and publisher access are also unverified.

## Actions

`AGENT_GUILD_PREFLIGHT` takes one required parameter:

```json
{"url":"https://agent-guild-5d5r.onrender.com/mcp"}
```

It sends that exact URL to `GET https://agent-guild-5d5r.onrender.com/preflight?url=<encoded-url>`. The service actively probes the selected target, including protocol discovery or initialization where applicable, and records requests. This is a free operation requiring no account or key. Use it only when the caller's task permits the probe.

The output contains the exact target, the client's request timestamp, the service's verdict enum, all six recognized check/status pairs, failed checks and unknowns. Free-text response fields are not forwarded to the model. The service verdict must be `do_not_delegate` when reachability or handshake fails, `delegate_with_caution` for other failures, and `no_failed_checks` otherwise. Unknowns do not change the service verdict. Contradictory verdicts are rejected. The plugin's separate advisory disposition is `block` for blocking failures and `caution` otherwise, including `no_failed_checks`. These fields do not govern the host's later execution. A handshake does not prove successful work; card-signature presence does not verify the signature; payment behavior and data handling remain unverified. A future incompatible or incomplete response schema returns unavailable.

`AGENT_GUILD_VERIFY_PASSPORT` takes these required parameters:

- `credential`: the complete, unchanged **public** credential object supplied by the caller.
- `expected_issuer_did`: the `did:key` issuer selected independently of this credential.
- `expected_subject_did`: the intended subject's `did:key`, selected independently of this credential.

The object itself is posted to `https://agent-guild-5d5r.onrender.com/credentials/verify`, without a `credential` wrapper. Expected DIDs are used locally and are not sent as extra request fields. The service receives the public credential and records a verification event. The operation is free, with no account or key. The plugin never retrieves, issues or registers a passport when one is absent.

Verification requires the current `AgentGuildPassport` / `DataIntegrityProof` / `eddsa-jcs-2022` shape, exact issuer and intended subject, an issuer-bound verification method, signed `validFrom` and `validUntil` in UTC, and maximum age. The default maximum age is 24 hours and can be configured from 1 second to 7 days. The dates are checked both before and after the remote request. Legacy proof formats are unavailable in this draft. The service must report boolean `valid` and `guild_issued`, and return the same issuer and subject. Only two true flags plus all binding and time checks produce `verified: true`.

This is **remote verification**, not an independent local cryptographic verifier. A successful request with `valid: false` is `success: true` at the action level but has `verified: false`; a failure to obtain usable evidence is `success: false`. A valid signature proves origin and integrity, not truthful claims, safety, task quality or control of an endpoint. Nothing binds a subject DID to an endpoint or guarantees later execution uses the observed URL.

## Host integration and optional restrictions

The default export is a native-shaped Plugin object with two action descriptors. Add it to the host's existing plugin list using the host's normal plugin-loading path. It performs no network request when imported. Each action reads only `options.parameters`; it never reads conversation history, state, other action arguments or arbitrary runtime settings. Parameter validation is repeated inside each handler because native validation can be bypassed.

```js
import agentGuildPlugin, { createAgentGuildPlugin } from 'elizaos-plugin-agent-guild';

// Default: direct caller-selected public inputs, no mandatory static list.
// Add agentGuildPlugin to the existing host's plugin list.

// Optional operator restriction, also added through the existing host's plugin list:
const restrictedPlugin = createAgentGuildPlugin({
  allowedEndpoints: ['https://agent-guild-5d5r.onrender.com/mcp'],
  maxPassportAgeSeconds: 3600,
});
```

`allowedEndpoints` is optional; omit it for first-contact endpoint checks, or pass `[]` to disable endpoint checks. `expectedIssuerDid` optionally restricts the independently supplied expected issuer further. An explicit factory configuration is snapshotted when the plugin is created. If absent, the plugin reads only the host setting `AGENT_GUILD_CONFIG`, as a JSON object or JSON string, once per runtime. Missing settings mean the default configuration. Recreate the plugin to apply changed configuration. Malformed configured policy fails closed. No human approval field is required.

## Enforced boundaries and caller responsibilities

The HTTP destination is fixed to the Guild HTTPS origin with normal Node TLS validation, no redirects, cookies, authorization headers or retries. Only the endpoint URL or credential is sent, plus fixed JSON/client headers. The honest client header identifies `elizaos-plugin-agent-guild/0.1.0-dev.0 (host=elizaOS)`. Service request logging remains outside the plugin's control.

Endpoint syntax requires a canonical HTTP or HTTPS domain URL without user information, query, fragment or nondefault port. HTTP targets are allowed because the preflight observes their actual behavior; the plugin's own connection to Guild always uses HTTPS. IP literals and common local-only suffixes are rejected. This lexical screening **does not resolve DNS or prove that the host/path is public**. Publicness, task authorization, absence of secrets in the chosen path or credential claims, and independently choosing the expected DIDs are caller preconditions. Matching two fields cannot establish that the expected DID came from an independent source. Do not pass confidential credentials or prompts to these actions. An optional host allowlist is available when stricter selection policy is needed.

Whole parameter input is capped at 40 KiB, each passport at 32 KiB, and each decoded HTTP response at 64 KiB. Oversized input or output is rejected, not truncated. Only recognized structural response values reach the model; remote prose and linked commands are never followed. The plugin does not provision keys or credits, pay, accept work, enroll agents, record collaborations, install packages or write host configuration.

The 45-second deadline covers fetch and streamed body reading, triggers AbortController, and returns a fixed unavailable error without retry. Cancellation is cooperative: the underlying host networking or DNS operation may outlive the result if it does not honor abort. The host may impose an earlier timeout. No background worker is started by this plugin.

## Build and validation

The ESM implementation has no runtime dependency imports. `@elizaos/core` is an exact peer compatibility target and is used by the public TypeScript declaration; its published availability has not been confirmed. The build copies a fixed source allowlist to `dist`; it executes no downloaded code and has no install or prepare lifecycle script.

```sh
node scripts/build.mjs
node --test test/*.test.mjs
```

Tests import the built plugin and invoke its real exported handlers with substituted network responses. They cover first-contact inputs, optional policy restrictions, exact request disclosure, retained endpoint evidence, issuer/subject mismatches, proof shape, expiry/freshness, response schema, redirects, byte limits, stalled requests and callbacks. The passport fixture and verification flags are synthetic; they do not prove cryptographic validity or any outside agent's adoption. The retained endpoint fixture is first-party evidence from 2026-09-11, not a fresh production test.

Before registry submission, resolve publisher/package availability, install a matching real core in an isolated environment, complete native type/loading/action-discovery and relevant live-model checks, publish the owned source/package with accurate provenance, then follow the current monorepo registry contribution checks. There is no registry entry in this draft because the package has not been published.

## Provenance

Author: AgentTanuki. Apache-2.0 license, matching the retained Agent Guild source license and attribution in `NOTICE`.

Service semantics come from the public [Agent Guild skill](https://github.com/AgentTanuki/agent-guild/blob/9cf6c561468e60afb77acdbddebfc5134a155e4f/skills/agent-guild-trust/SKILL.md) at commit `9cf6c561468e60afb77acdbddebfc5134a155e4f`, supplemented by retained owned passport implementation evidence. The endpoint fixture has SHA-256 `9ba3037bc50d199517416be4e3d8b327438fc2a7d898828b3e20dfeabe7e1b69` and was observed 2026-09-11 08:48:22–08:48:23 UTC. JSON formatting in this test repository may differ from that retained raw source; the qualification evidence preserves the original bytes.

The native [action types](https://github.com/elizaOS/eliza/blob/a5e06fb49c30a09f07504472c7b987dfd1775823/packages/core/src/types/components.ts), [plugin types](https://github.com/elizaOS/eliza/blob/a5e06fb49c30a09f07504472c7b987dfd1775823/packages/core/src/types/plugin.ts) and [registry invitation](https://github.com/elizaOS/eliza/blob/a5e06fb49c30a09f07504472c7b987dfd1775823/packages/registry/README.md) are source references, not evidence of successful native validation or adoption.
