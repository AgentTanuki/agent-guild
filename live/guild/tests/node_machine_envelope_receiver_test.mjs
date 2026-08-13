import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { canon } from "../../../sdk/agentguild_verify.mjs";
import { didKeySigner } from "../../../sdk/agentguild_envelope_client.mjs";
import {
  A2A_MACHINE_ENVELOPE_EXTENSION_URI,
  MACHINE_ENVELOPE_METADATA_KEY,
  a2aEnvelopeContext,
  a2aExtensionActivated,
  a2aMessageBytes,
  createAgentGuildA2AEnvelopeExtension,
  createAgentGuildMachineEnvelopeReceiver,
  createMemoryEnvelopeReplayStore,
} from "../../../sdk/integrations/machine_envelope_receiver.mjs";

const guild = didKeySigner("22".repeat(32));
const sender = didKeySigner("11".repeat(32));
const recipient = "did:key:z6MkReceiver";
const now = new Date("2026-08-13T00:00:00.000Z");

async function issueFor(message, overrides = {}) {
  const signed = {
    type: "AgentGuildMachineEnvelope",
    version: 1,
    id: "urn:sha256:" + createHash("sha256")
      .update(String(overrides.idSeed || message.messageId)).digest("hex"),
    protocol: "agent-guild/machine-envelope/v1",
    issuer: guild.did,
    issued_at: now.toISOString(),
    valid_until: new Date(now.getTime() + 3_600_000).toISOString(),
    sender: {
      did: sender.did,
      authentication: "agent-guild/caller-proof/v1",
      caller_proof_verified: true,
    },
    message: {
      kind: "delegation",
      recipient,
      payload_sha256: createHash("sha256")
        .update(a2aMessageBytes(message)).digest("hex"),
      nonce: "receiver-test-nonce-0001",
      context: a2aEnvelopeContext(message),
      ...overrides.message,
    },
    attestation_scope: { attested: "test", not_attested: [], privacy: "test" },
    verification: { suite: "eddsa-jcs-2022" },
  };
  const proof = Buffer.from(
    await guild.sign(Buffer.from(canon(signed), "utf8")),
  ).toString("hex");
  const envelope = { ...signed, proof };
  envelope.envelope_sha256 = createHash("sha256")
    .update(canon(envelope)).digest("hex");
  return envelope;
}

function baseMessage(messageId = "message-0001") {
  return {
    kind: "message",
    role: "user",
    messageId,
    parts: [{ kind: "text", text: "Delegate code review task 42" }],
  };
}

assert.throws(
  () => createAgentGuildMachineEnvelopeReceiver({ recipient }),
  /expectedIssuers must pin/,
);

const missingMessage = baseMessage("missing-0001");
const gate = createAgentGuildMachineEnvelopeReceiver({
  recipient,
  expectedIssuers: [guild.did],
  replayStore: createMemoryEnvelopeReplayStore({ now: () => now.getTime() }),
  marketplaceUrl: "https://payanagent.com/x402/kh-test",
  now: () => now,
});
const missing = await gate.authorizeA2AMessage(missingMessage);
assert.equal(missing.authorized, false);
assert.equal(missing.code, "machine_envelope_required");
assert.equal(missing.acquisition.recipient, recipient);
assert.equal(missing.acquisition.marketplace, "https://payanagent.com/x402/kh-test");

const message = baseMessage();
message.metadata = { [MACHINE_ENVELOPE_METADATA_KEY]: await issueFor(message) };
const allowed = await gate.authorizeA2AMessage(message);
assert.equal(allowed.authorized, true);
assert.equal(allowed.senderDid, sender.did);
assert.equal(allowed.kind, "delegation");

const replay = await gate.authorizeA2AMessage(message);
assert.equal(replay.authorized, false);
assert.equal(replay.code, "machine_envelope_replayed_or_expired");

const tampered = structuredClone(message);
tampered.messageId = "tampered-0002";
const tamperedResult = await createAgentGuildMachineEnvelopeReceiver({
  recipient,
  expectedIssuers: [guild.did],
  replayStore: createMemoryEnvelopeReplayStore({ now: () => now.getTime() }),
  now: () => now,
}).authorizeA2AMessage(tampered);
assert.equal(tamperedResult.authorized, false);
assert.match(tamperedResult.code, /payload_mismatch|context_mismatch/);

const wrongRecipientMessage = baseMessage("wrong-recipient-0001");
wrongRecipientMessage.metadata = {
  [MACHINE_ENVELOPE_METADATA_KEY]: await issueFor(wrongRecipientMessage, {
    message: { recipient: "did:key:z6MkSomeoneElse" },
  }),
};
const wrongRecipient = await createAgentGuildMachineEnvelopeReceiver({
  recipient,
  expectedIssuers: [guild.did],
  replayStore: createMemoryEnvelopeReplayStore({ now: () => now.getTime() }),
  now: () => now,
}).authorizeA2AMessage(wrongRecipientMessage);
assert.equal(wrongRecipient.code, "machine_envelope_wrong_recipient");

const wrongIssuerGate = createAgentGuildMachineEnvelopeReceiver({
  recipient,
  expectedIssuers: [sender.did],
  replayStore: createMemoryEnvelopeReplayStore({ now: () => now.getTime() }),
  now: () => now,
});
const wrongIssuerMessage = baseMessage("wrong-issuer-0001");
wrongIssuerMessage.metadata = {
  [MACHINE_ENVELOPE_METADATA_KEY]: await issueFor(wrongIssuerMessage),
};
const wrongIssuer = await wrongIssuerGate.authorizeA2AMessage(wrongIssuerMessage);
assert.equal(wrongIssuer.code, "machine_envelope_untrusted_issuer");

assert.equal(a2aExtensionActivated({
  "A2A-Extensions": `https://example.test/other, ${A2A_MACHINE_ENVELOPE_EXTENSION_URI}`,
}), true);
assert.equal(a2aExtensionActivated({ "A2A-Extensions": "https://example.test/other" }), false);

let adapterVerificationCalls = 0;
const adapterReceiver = createAgentGuildMachineEnvelopeReceiver({
  recipient,
  verifyEnvelope: async (envelope) => {
    adapterVerificationCalls += 1;
    return {
      valid: true,
      issuer: envelope.issuer,
      senderDid: envelope.sender.did,
    };
  },
  replayStore: createMemoryEnvelopeReplayStore({ now: () => now.getTime() }),
  now: () => now,
});
const adapter = createAgentGuildA2AEnvelopeExtension({
  receiver: adapterReceiver,
  acquisitionUrl: "https://receiver.test/a2a/extensions/machine-envelope/v1/acquire",
  kind: "delegation",
});
assert.equal(adapter.agentCardExtension.uri, A2A_MACHINE_ENVELOPE_EXTENSION_URI);
assert.equal(adapter.agentCardExtension.params.activation_header, "A2A-Extensions");
assert.match(adapter.agentCardExtension.params.message_field_role, /optional/);

const headerOnlyMessage = baseMessage("header-only-0001");
const inactive = await adapter.authorizeA2ARequest({
  headers: {},
  message: headerOnlyMessage,
});
assert.equal(inactive.code, "machine_envelope_extension_required");
assert.equal(inactive.activated, false);
assert.deepEqual(inactive.responseHeaders, {});
assert.equal(adapterVerificationCalls, 0, "inactive extension must not verify an envelope");

const activatedMissing = await adapter.authorizeA2ARequest({
  headers: { "a2a-extensions": A2A_MACHINE_ENVELOPE_EXTENSION_URI },
  message: headerOnlyMessage,
});
assert.equal(activatedMissing.code, "machine_envelope_required");
assert.equal(activatedMissing.activated, true);
assert.equal(
  activatedMissing.responseHeaders["A2A-Extensions"],
  A2A_MACHINE_ENVELOPE_EXTENSION_URI,
);
assert.equal(adapterVerificationCalls, 0);

headerOnlyMessage.metadata = {
  [MACHINE_ENVELOPE_METADATA_KEY]: await issueFor(headerOnlyMessage),
};
const activatedAllowed = await adapter.authorizeA2ARequest({
  request: new Request("https://receiver.test/a2a", {
    headers: { "A2A-Extensions": A2A_MACHINE_ENVELOPE_EXTENSION_URI },
  }),
  message: headerOnlyMessage,
});
assert.equal(activatedAllowed.authorized, true);
assert.equal(activatedAllowed.activated, true);
assert.equal(adapterVerificationCalls, 1);

const boundedStore = createMemoryEnvelopeReplayStore({
  maxEntries: 1,
  now: () => now.getTime(),
});
assert.equal(await boundedStore.consume({
  key: "first",
  expiresAt: new Date(now.getTime() + 60_000),
}), true);
assert.equal(await boundedStore.consume({
  key: "second",
  expiresAt: new Date(now.getTime() + 60_000),
}), false, "capacity exhaustion must not evict an unexpired replay key");
assert.equal(await boundedStore.consume({
  key: "first",
  expiresAt: new Date(now.getTime() + 60_000),
}), false, "the first envelope remains consumed after capacity exhaustion");

console.log("machine envelope receiver gate tests passed");
