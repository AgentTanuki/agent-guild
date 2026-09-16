# Agent Guild Endpoint Observations

Use **Observe public endpoint** when a workflow is considering a newly selected public service and wants endpoint observations before making a separate connection decision. A bot that uses only known services may have no need for this action.

## Use the action

1. Select **Observe public endpoint** in your workflow.
2. Supply the exact public HTTP or HTTPS endpoint in `url`, for example `https://worker.example.org/mcp`. This example is illustrative; it is not a service to call.
3. Inspect `status`, `checks`, `failed`, `unknowns`, and `limitations`. Decide separately whether to connect, send data, or delegate. The action does none of these for you.

There are no integration configuration fields and no Guild account or API key. Only the free Guild preflight operation is used. Botpress account, workspace, hosting, and usage requirements are separate; this integration makes no claim about their cost.

## What is sent

The action makes one GET request to `https://agent-guild-5d5r.onrender.com/preflight`, with the selected URL encoded in its `url` query parameter and a fixed integration identifier. The action handler reads only the selected URL. It does not include conversation history, Botpress framework context, credentials, or other tool data in the Guild request. Do not supply URLs containing tokens, private resource identifiers, or confidential query values. Publicness is a precondition of the caller's task, not something this action can establish.

Guild receives the full selected URL and may log the request. Guild's preflight service can make bounded requests to that endpoint to collect observations. This is an active observation, not a passive directory lookup. The integration itself connects only to the fixed Guild origin; it never invokes the selected service's tools or performs payment.

## Interpreting the result

An `observed` result means a complete, structurally consistent service report matched the selected URL exactly. It is not independent verification and does not mean the service is safe. It contains these six check names and their service-reported `proven`, `failed`, or `unknown` status:

- `endpoint_reachable`
- `protocol_handshake`
- `agent_card_resolves`
- `agent_card_signed`
- `payment_claim_holds`
- `independent_evidence`

A failed reachability or handshake check corresponds to `do_not_delegate`; another failed check corresponds to `delegate_with_caution`; no failed checks corresponds to `no_failed_checks`. Unknown checks remain unknown and are excluded from that calculation, including when every check is unknown. The integration rejects inconsistent verdicts or summary arrays. None of these values grants permission to delegate or pay.

Card-signature presence is not cryptographic signature verification. Payment claims do not prove settlement. Reported evidence does not establish independent ownership, task success, secure data handling, or a binding between identity and later execution. Local request/completion timestamps record when this observation was collected; they are not signed service timestamps and do not guarantee freshness at a later decision.

Only the six statuses, consistent summary arrays, exact target, local request metadata, and fixed interpretation limits are returned. Remote explanatory prose, links, instructions, and other fields are omitted.

`rejected` means the selected input was unsupported. `unavailable` means no usable observation was obtained, including transport failure, oversized or malformed responses, missing checks, target mismatch, or contradictory service evidence. In either case the verdict is null and check arrays are empty. There is no retry, payment, registration, or alternate-service fallback.

## Boundaries

URLs are limited to 2,048 characters. HTTP(S) DNS names are supported; IP literals, single-label names, reserved local suffixes, credentials, non-empty fragments, whitespace, and malformed Unicode text are rejected. Host screening uses standard URL parsing and normalization, while the original URL is transmitted unchanged. Percent-encoded ASCII hostnames and an empty trailing fragment marker can be accepted. This is lexical screening, not DNS resolution or a complete SSRF defense. A public-looking name can still resolve privately; the observing service must enforce its own destination policy.

A request has a 15-second action deadline and a 65,536-byte limit on the decoded response stream read by the integration, not on compressed wire traffic or all runtime memory. Redirects are rejected. Cancellation uses native Fetch's AbortSignal and stream cancellation. If a host replaces Fetch with an implementation that ignores cancellation, underlying work may continue after the action returns; cancelling cannot undo requests already received by Guild or its endpoint observation.

This is an optional workflow action, not an automatic guard on other integrations or MCP connections. Operators remain responsible for their workflow's decisions and data-disclosure policy.
