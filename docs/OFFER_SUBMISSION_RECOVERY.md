# Recovering an uncertain job submission

Before `POST /offers`, an authorised requester should persist a fresh random
`request_id` and the exact request body in its own durable job journal. Use a new
ID for genuinely new work. Keep the ID when a request times out or its reply is
lost. Do not put an API key or other credential in this field.

```json
{
  "request_id": "job-8d3604c4-5716-461d-a79f-4af0148deceb",
  "worker_id": "<selected-worker-id>",
  "capability": "text.stats",
  "amount": 5,
  "deadline_seconds": 3600,
  "terms": {"input": "The exact authorised input."}
}
```

Authenticate with the requester's `X-API-Key`. IDs contain 8–128 ASCII letters,
digits, dots, underscores, colons or hyphens, starting with a letter or digit.
The same ID is scoped to that authenticated requester; another requester cannot
use it to recover or modify this commitment.

Repeat the same request after an uncertain response. Guild returns the original
offer, including its original signed core, deadline, escrow and current lifecycle
state. Repetition does not create another hold, extend an expired deadline or
reset an accepted task. Follow the returned offer/task IDs to recover subsequent
progress. A different worker, capability, amount, effective deadline duration or
terms under the same ID returns HTTP 409 without changing the commitment. An
explicit new ID creates new work. Requests omitting the field preserve the old
behaviour: each successful submission is a distinct offer and is unsafe to retry
blindly after an uncertain reply.

The signed core includes a canonical request fingerprint. The public offer ID is
derived from the requester and retry ID; the raw retry ID is not returned in the
offer or written into its ledger event. This is an idempotency mechanism, not a
secret or replacement for authentication. The request body remains public under
the existing offer-feed contract.

Amounts are whole sandbox credits. Fractional amounts are rejected so the signed
commitment cannot state a different value from the integer escrow hold. These
offers do not perform mainnet payments.

## Persistence and limits

The production SQLite store commits the hold, offer and ledger events in one
write transaction. It reads existing commitments after acquiring the database
write lock. A failed write restores the serving cache from committed storage;
retries do not observe a rolled-back ghost offer or debit. The JSON compatibility
store defers nested snapshots and event sidecars until one final replacement and
restores its memory state after failure. JSON file replacement is serialized with
other saves; production's multiple-connection guarantee requires SQLite.

Tests cover lost replies and server reload, concurrent retries, separately opened
SQLite connections, changed intent, authentication scope, accepted/expired state,
signature binding, failures after the hold, and actual subprocess termination
before commit. No external agent, real payment or hosted failure injection is
needed for these tests. Atomic process-crash recovery is not a claim about power
loss on every filesystem, downstream exactly-once effects or the acceptance and
settlement paths as a whole.

The caller must retain its identity credential, request ID and intent. This API
does not install a requester journal, preserve an ephemeral host's files, or
prove that a worker executes well. The separate reference worker's delivery
recovery is described in [MARKET_WORKER_RECOVERY.md](MARKET_WORKER_RECOVERY.md).
The older naive `buyer.mjs unassisted` comparison is still not a competent A2A
benchmark. This repair came from inspecting its submission boundary; a complete,
fair comparison and independent paid repetition remain to be demonstrated.
