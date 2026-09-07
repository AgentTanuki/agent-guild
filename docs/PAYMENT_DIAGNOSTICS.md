# Payment-stage observations

`GET /commercial` includes `payment_diagnostics` (`payment-stages-v1`). Its
existing `operation` filter scopes these observations. `/billing/revenue` remains
the authoritative settlement ledger for money.

These are prospective observations for invocations presenting a payment
credential or an A2A payment-submission message. Ordinary unpaid reads, free
discovery and sandbox-only requests do not create diagnostic rows. Existing
challenge reports continue to cover that traffic. Each invocation gets a fresh
random identifier, including retries. The public summary returns aggregate rows,
not identifiers, buyer counts or conversion rates.

| Stage | Observed fact |
|---|---|
| `payment_submission_present` | An A2A submission reached the handler, possibly without a valid payload or task. |
| `credential_present` / `credential_parsed` | A carrier was present / decoded. Neither proves signature validity or buying intent. |
| `credential_ignored` | The payment rail was disabled; existing fallback behavior is preserved. |
| `binding_valid` | The shared gateway accepted exact request, amount, asset, recipient and validity binding. |
| `facilitator_verify_started` / `facilitator_verified` | Verification was called / reported validity. |
| `facilitator_settle_started` / `facilitator_settlement_accepted` | Settlement was called / reported success with a well-formed transaction hash. This is not independent confirmation. |
| `chain_confirmation_started` / `chain_confirmed` | Independent confirmation ran / confirmed settlement. Recovery need not contact the facilitator again. |
| `recovery_started` | The gateway resumed a durable payment record. |
| `authorization_accepted` | Result production was authorized. Testnet authorization is not revenue. |
| `result_prepared` / `cached_result_prepared` | Result bytes and receipt were prepared, or a cached result was recovered. This does not prove delivery or consumption. |
| `rejected` / `unresolved` | A fixed reason code records refusal or uncertainty. Diagnostics never authorize a retry or charge. |

A malformed carrier can be refused while an existing sandbox fallback still
authorizes the request. `sandbox_authorized` / `free_authorized` identify those
outcomes when a payment carrier was present. `request_failed` and
`result_finalize_failed` record server failures observable in that context.

Only fixed codes enter the stream: no exception text, credentials, bodies, URLs,
request hashes, wallets, DIDs or client labels. Traffic is known first-party or
unclassified; unclassified is not proved independently owned. Writes are best
effort and cannot raise into payment processing.

Coverage begins at priced gateways, excluding earlier routing, schema and
caller-proof failures. No history is backfilled. Crashes, lost executor context
or storage failure can leave gaps. Zero observed credentials does not establish
no wallet, no budget or price rejection. Recovery and cached replay do not repeat
every stage. Do not divide aggregate stages into a conversion rate. Storage-history
completeness is reported separately from these observation limits.

Passport activity now advertises `passport-activity-v2`: the successful-event
counter maps the real `passport_issued` event, uses durable history and aggregates
issuer failure reasons separately from voluntary abandonment. Earlier retained
snapshots are not directly comparable. A higher corrected count is not new adoption.
