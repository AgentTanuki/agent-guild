# Discovery Swarm — Threat Model

All external agents, tools, registries, and supplied content are untrusted.

## Assets
Guild signing key (custodial DID), agent api_keys, admin token, reputation graph integrity, ledger integrity, growth metrics integrity (product evidence), Render compute/disk, Guild's standing in external registries, Ross's identity (never expose "rwdburley"; publish as AgentTanuki only).

## Threats and mitigations

| # | Threat | Mitigation |
|---|---|---|
| T1 | Prompt injection via invocation payloads / A2A messages | Guest capabilities are deterministic pure functions — payloads are parsed as data, never interpreted as instructions; no LLM in the guest invoke path in Pilot A. A2A responder already treats text as intent keywords only |
| T2 | Tool poisoning / malicious manifests from external registries | Mapper adapters are read-only; fetched registry content is never executed and never mutates our capability registry; identities only enter via in-repo templates + fixture gate |
| T3 | Credential theft | No secrets in identity docs, envelopes, or ledger (existing invariant); guest tier requires no credentials; member keys only over HTTPS headers; secrets stay in Render env / `live/secrets` (gitignored) |
| T4 | Recursive delegation loops (agent invokes AG which invokes agent…) | Guest capabilities make no outbound calls at all in Pilot A. Discovery agents call only whitelisted external GET/POST endpoints with per-target circuit breakers |
| T5 | Unbounded spending | Guest capabilities cost ~$0 compute; discovery-agent actions carry per-day action budgets (hard caps in code); no external action type with monetary cost is whitelisted; Stripe untouched |
| T6 | Data exfiltration via invocation inputs | Payload size caps (64 KB); inputs not persisted raw — experience records store shape statistics only; data-retention statement in every envelope |
| T7 | SSRF | No capability fetches URLs. citation/url checks are purely syntactic. Discovery-agent targets come from a static allowlist in the ecosystem map, https-only, no private IP ranges |
| T8 | Arbitrary code execution | No eval/exec; regex-extract bounds pattern length, input length, and match count (and rejects catastrophic constructs); JSON parsing via stdlib with depth/size caps |
| T9 | DoS on guest tier | Per-actor daily token buckets + global per-minute breaker + payload caps + 429 with Retry-After; kill switch halts all guest invocations instantly |
| T10 | Sybil / referral fraud (fake externals farming referral rewards) | Existing: activation-gated rewards, caps, collusion detection, first-party exclusion. New: referral tokens attribute but never pay automatically; graph labels organic vs internal via `is_genuine_external` |
| T11 | Reputation manipulation via swarm identities | Swarm identities are `first_party=true`, `seed=false`, tagged `swarm_identity`; excluded from growth metrics and from genuine-external funnels; they do not attest each other |
| T12 | False demand / manufactured ecosystem evidence | Growth metrics computed only over attribution-verified external actors; AG-owned traffic excluded by construction; dashboard shows external vs internal side-by-side; E2E simulator is labelled first-party |
| T13 | Agents falsely claiming AG membership | Existing: signed passports, badges, `/credentials/verify`, offline verifiers. Envelopes are Guild-signed and verifiable against `/.well-known/agent-guild-did.json` |
| T14 | Registry ToS violations / spam | Only documented registration/submission methods; human-approval-required targets stay draft-only (`blocked_needs_human`); no mass-messaging, no fake human accounts, robots respected; rate limits per ecosystem map |
| T15 | Discovery agent privilege escalation | Discovery agents execute only registered action types through one policy chokepoint (rate limit → kill switch → allowlist → log); no shell/deploy/DB-write capability exists in their interface |
| T16 | Kill-switch failure | Checked at the top of every invoke and every swarm action; env override works without store access; covered by tests |
| T17 | Dependency attacks | New deps limited to `jsonschema`, `python-dateutil` (both mature, pinned minimums); no post-install hooks; everything else stdlib |
| T18 | Learned-behaviour drift | Skill Objects are offline-proposed, held-out-tested, versioned, human-reviewable; no production agent rewrites its own permissions/objectives/limits |

## Residual risks
Render single instance = availability SPOF (accepted for pilot). Custodial Guild key on one host (existing risk, unchanged). Guest tier is anonymous by design — abuse detection is rate/shape-based only in Pilot A.
