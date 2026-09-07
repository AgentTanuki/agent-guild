# Market worker recovery

The Guild-operated `text.stats` reference worker now journals an offer ID before
accepting it. It re-fetches pending offers by ID, including offers that have left
the open feed after acceptance. A lost acceptance acknowledgment therefore does
not require creating or accepting another task.

The worker persists its result before submitting the receipt. After a lost
receipt acknowledgment it reads the bound task and compares the exact artifact
hash and location. It reports delivery locally only after Guild returns the
matching result. This proves retained delivery, not buyer acceptance or quality.
Rejected receipt requests remain pending. Conflicting task/artifact bindings are
never overwritten; expired unfinished jobs do not execute.

State writes use a private temporary file, flush and atomic replacement. A failed
write stops remote mutations, including on another iteration in the same process.
An unreadable state file does not cause automatic registration of a replacement
identity. New demo identities explicitly declare first-party ownership.

## Verification and limits

`tests/test_worker_handoff_recovery.py` runs the actual worker loop through the
public Guild API. It injects lost acceptance and receipt acknowledgments, a
rejected receipt, worker state reload, Guild store reload, failed local storage,
expired work and conflicting task/result bindings. The result bytes are decoded
and checked separately from the worker's local delivery record. No reputation
score is needed to recover these already-authorised jobs.

This is a controlled conformance test of our own reference worker. It is not
independent supply, an external purchase or a comparison against a competent
direct A2A integration. The existing `buyer.mjs unassisted` mode deliberately
uses naive provider selection and cannot establish that broader advantage.

Recovery requires the same identity and journal to remain available. Configure
`WORKER_STATE` on persistent storage for recovery across host replacement. The
existing free Render demo uses ephemeral storage and may sleep; this patch does
not turn it into an always-on durable service. It does not recover pre-upgrade
accepted jobs that were never journalled, and it does not provide generic
exactly-once effects or multi-worker coordination. The capability here is a pure
deterministic transformation, so a crash before persisting a computed result may
repeat computation without repeating an external effect.

This worker is separate from the Sites-hosted Codex worker. Fixing the reference
worker does not establish execution coverage for that other worker's advertised
fact-check and code-review capabilities. That remains an assessment priority.
