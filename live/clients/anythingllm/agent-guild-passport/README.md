# Agent Guild Passport — offline verification

An optional AnythingLLM agent skill by **AgentTanuki**. It checks a supplied public
Agent Guild Passport's Ed25519 signature, an administrator-pinned issuer, an
independently known subject DID and signed freshness. It makes no network calls,
issues no identity, performs no payment and returns no automatic hire decision.

## Configure and use

1. Place this entire `agent-guild-passport` folder inside your AnythingLLM storage
   directory's `plugins/agent-skills/` folder. The folder name must match `hubId`
   in `plugin.json`. These flat files require Node.js 18+ and no npm dependencies.
2. In the skill settings, configure `EXPECTED_ISSUER_DID` using a trusted issuer
   DID established independently of the passport being checked. There is no
   bundled production pin, automatic discovery or trust-on-first-use. An absent
   or invalid pin produces `unavailable`; a caller cannot override it through
   the skill's invocation arguments.
3. Enable the initially inactive skill. Reload the skills page if needed and
   exit an existing agent session before expecting a newly added skill to load.
4. In an `@agent` task, supply a **shareable public passport** as complete JSON
   text and the subject's Ed25519 `did:key` already known from the task. Ask for
   offline verification. Do not derive the expected subject from the passport
   merely to make the comparison pass.

The invocation has exactly two string parameters:

```json
{
  "credential_json": "<complete public AgentGuildPassport JSON text, including proof>",
  "expected_subject_did": "<independently known subject did:key>"
}
```

These are placeholders, not a valid credential. The administrator's issuer pin
is configuration, not a third invocation parameter. The skill can compare
bindings; it cannot establish that the administrator's pin is trustworthy or
that the model obtained the expected subject independently. Those are local
operator/task preconditions.

## Result and policy

The handler always returns a concise JSON **string**. `status: "verified"` and
`verified: true` mean all of this skill's checks passed for the supplied snapshot:

- A single VC 2.0 context, the current `VerifiableCredential` /
  `AgentGuildPassport` type pair, a subject and a credential identifier.
- A `DataIntegrityProof` using `eddsa-jcs-2022`, `assertionMethod`, matching proof
  context and the issuer's exact Ed25519 `did:key` verification method.
- Exact issuer/subject matches and a valid signature over every signed member.
- Valid timezone-qualified `validFrom` and `proof.created` timestamps describing
  the same instant; neither is in the future. Their age is **at most 24 hours**.
  This is a fixed local consumption policy, stricter than the service's default
  seven-day passport lifetime. There is no future-clock allowance; a correctly
  set host clock is required. `validUntil`, if present, must be later than
  `validFrom` and strictly later than the current time. The current service
  supplies `validUntil`; its low-level emitter also supports omitting it.

`rejected` means an input, binding, time, structure or signature check failed;
`unavailable` means configuration or an unexpected local failure prevented a
result. `verified: false` does not by itself distinguish an unchecked signature
from an invalid one; the fixed `reason` identifies the failed stage. Rejection
is not a claim that the counterparty is malicious. Examples of reasons include
`issuer_mismatch`, `subject_mismatch`, `snapshot_too_old`, `expired` and
`invalid_signature`. No raw exceptions, passport claims, identifiers or remote
prose are returned.

Inputs are limited to 65,536 UTF-8 bytes, 16 nested value levels and 4,096 JSON
values. Duplicate keys (including escaped aliases), malformed Unicode,
non-ASCII field names, non-finite/unsafe numeric values and decimal spellings
that would silently round to a different JSON number are rejected. Unicode
string values are supported. The verifier retains the pinned SDK's canonical
JSON algorithm; unusual representations outside its cross-language subset may
fail verification. Historical legacy proofs, other VC types and cryptosuites
are deliberately unsupported. Timestamp precision is up to six fractional
digits, matching the current emitter. These are scoped interoperability limits,
not a general-purpose W3C credential verifier.

## Meaning and privacy limits

A successful result establishes origin and integrity of a recent signed
snapshot. It does **not** establish endpoint ownership, current reputation,
independent operation, truth of every signed claim, safety, work quality or
permission to act. This skill does not check revocation, live reputation,
checkpoint signatures or ledger inclusion. It does not attach another agent,
intercept later tools or govern the agent's next action.

“Offline” describes **this skill's verifier**. It neither sends the passport to
Guild nor records a Guild verification event. It does not call the host logger,
introspection or network helpers. AnythingLLM and its configured model/provider
may already receive, store or log the task and tool arguments under host
settings. Do not supply a secret or private credential; this is not a whole-agent
privacy guarantee.

## Distribution status

The source example is prepared for manual installation and optional future Hub
publication. Local tests cover the handler and the pinned AnythingLLM
registration/schema/CLI validation surfaces; no AnythingLLM UI, model session,
Hub upload or hosted installation has been verified. The inspected Hub CLI
rewrites `hubId`, removes setup values and also drops `schema`/`imported` while
preparing its upload manifest. How the Hub completes that manifest is untested;
inspect the eventual downloaded artifact before enabling it. A successful local
validator run does not establish a published, installable or verified Hub item.

See [PROVENANCE.md](PROVENANCE.md) and [LICENSE.txt](LICENSE.txt). Reproducible
source tests and the deterministic adaptation script live in the parent source
example; they are not part of this flat upload folder.
