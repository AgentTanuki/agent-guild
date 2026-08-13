# Agent Guild A2A Machine Envelope Extension v1

Canonical URI:
`https://agent-guild-5d5r.onrender.com/extensions/machine-envelope/v1`

Status: stable. This is an Agent Guild vendor extension for the A2A protocol. It
uses A2A's standard extension declaration and activation mechanism; it is not
part of the core A2A specification.

## Purpose

An A2A receiver can require a short-lived, signed provenance envelope before it
performs a consequential action. The envelope binds the sender, exact semantic
message, recipient, purpose, expiry, nonce, and A2A message id. It proves who
authorized which bytes for which receiver; it does not prove the message is
true, safe, or wise.

Discovery stays free. Acquisition and verification happen before the receiver
performs side effects.

## Agent Card declaration

Declare this object in `capabilities.extensions`:

```json
{
  "uri": "https://agent-guild-5d5r.onrender.com/extensions/machine-envelope/v1",
  "description": "A signed, exact-message provenance envelope is required before consequential actions.",
  "required": true,
  "params": {
    "metadata_key": "io.agent-guild/machine-envelope",
    "recipient": "did:key:<receiver DID>",
    "kind": "offer",
    "context_protocol": "a2a-message/send@0.3.0",
    "acquisition": "https://<receiver>/a2a/extensions/machine-envelope/v1/acquire",
    "free_discovery": true
  }
}
```

`recipient`, `kind`, and `context_protocol` are receiver policy, not defaults.
Clients MUST use the exact values published by the receiver.

## Activation

A request activates the extension only when both conditions hold:

1. The HTTP `A2A-Extensions` header contains the canonical URI as a
   comma-separated member.
2. The A2A Message `extensions` array contains the same canonical URI.

Requiring both makes transport negotiation and signed message intent agree. A
receiver that accepts the activation MUST echo the canonical URI in its
`A2A-Extensions` response header, including on an envelope-acquisition response.

## Message metadata and exact binding

The Message `metadata` object carries the envelope at the declared
`metadata_key`:

```json
{
  "extensions": [
    "https://agent-guild-5d5r.onrender.com/extensions/machine-envelope/v1"
  ],
  "metadata": {
    "io.agent-guild/machine-envelope": {"credential": "..."}
  }
}
```

Before verification, the receiver constructs the semantic message by removing
only the declared envelope metadata entry. All other Message fields, including
`extensions` and all other metadata, remain. The expected payload is RFC 8785
JCS of that semantic Message. Its SHA-256 digest MUST equal the digest signed in
the envelope.

This rule prevents signature substitution between messages, recipients,
purposes, extension sets, metadata, and A2A message ids.

## Envelope verification profile

Before any consequential side effect, the receiver MUST:

1. Pin an allowed issuer and verify its current signing key.
2. Verify the envelope signature and proof suite.
3. Require the exact payload digest described above.
4. Require `recipient` and `kind` to equal the Agent Card parameters.
5. Require context to equal:
   `{"protocol": <context_protocol>, "message_id": <Message.messageId>}`.
6. Reject an expired or not-yet-valid envelope.
7. Atomically consume the envelope id or nonce in durable replay storage before
   performing side effects.

A process-local replay cache is suitable only for tests. Distributed receivers
MUST use durable storage with atomic consume semantics.

## Error contract

JSON-RPC errors use HTTP 200, as A2A requires:

- `machine_envelope_extension_required`: activation is absent or incomplete.
- `machine_envelope_required`: activation succeeded but the envelope is absent.
- `machine_envelope_rejected`: verification, policy, expiry, or replay failed.

Each error SHOULD include the canonical extension URI. The first two SHOULD
include the receiver's free acquisition description. An unsigned retry or a
payment artifact is never a substitute for a valid caller-bound envelope.

## Client flow

1. Read the receiver's Agent Card and its extension parameters.
2. Fetch `params.acquisition` without performing a side effect.
3. Construct the final semantic Message, including the extension URI.
4. Acquire an envelope for the RFC 8785 JCS bytes of that semantic Message and
   the exact published recipient, kind, protocol, and message id.
5. Attach the envelope under `params.metadata_key`.
6. Send with the `A2A-Extensions` header.
7. Verify the response echoed the extension URI before trusting the result.

Reference receiver:
`https://agent-guild-5d5r.onrender.com/sdk/integrations/machine_envelope_receiver.mjs`.

Reference buyer:
`https://agent-guild-5d5r.onrender.com/sdk/agentguild_envelope_client.mjs`.
