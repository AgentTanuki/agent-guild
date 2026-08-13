// Agent Guild receiver-side gate for consequential machine messages.
//
// Discovery can remain free. Before a receiver performs a consequential action,
// call `authorize(...)` (generic bytes) or `authorizeA2AMessage(...)` (A2A 0.3).
// The gate verifies Guild provenance, exact payload/recipient/purpose binding,
// expiry, transport context, and single use. It never treats an envelope as an
// attestation that a message is true or safe to execute.
//
// Zero npm dependencies. Offline mode uses the reviewed Agent Guild verifier:
//
//   const gate = createAgentGuildMachineEnvelopeReceiver({
//     expectedIssuers: ["did:key:z6Mk..."],
//     recipient: "did:key:z6MkReceiver...",
//     replayStore: durableAtomicReplayStore,
//   });
//   const result = await gate.authorizeA2AMessage(message);
//   if (!result.authorized) return result; // fail closed before side effects
//
// `replayStore.consume({key, expiresAt})` MUST atomically return true only for
// the first use. The included memory store is suitable for tests and one-process
// agents; distributed receivers must provide a durable atomic implementation.
// Build the standalone served file with:
//   esbuild sdk/integrations/machine_envelope_receiver.entry.mjs \
//     --bundle --platform=node --format=esm --target=node18 \
//     --outfile=sdk/integrations/machine_envelope_receiver.mjs

import { createHash } from "node:crypto";
import {
  DEFAULT_HOST,
  canon,
  verifyMachineEnvelope,
} from "../agentguild_verify.mjs";

export const MACHINE_ENVELOPE_METADATA_KEY =
  "io.agent-guild/machine-envelope";
export const A2A_ENVELOPE_CONTEXT_PROTOCOL =
  "agent-guild/a2a-message-binding/v1";

const DEFAULT_KINDS = Object.freeze([
  "intent",
  "delegation",
  "authorization",
  "acceptance",
  "delivery",
  "payment",
]);

function sha256Hex(value) {
  return createHash("sha256").update(value).digest("hex");
}

function asBytes(value, label = "payload") {
  if (typeof value === "string") return Buffer.from(value, "utf8");
  if (value instanceof ArrayBuffer) return Buffer.from(value);
  if (ArrayBuffer.isView(value)) {
    return Buffer.from(value.buffer, value.byteOffset, value.byteLength);
  }
  throw new TypeError(`${label} must be a string, ArrayBuffer or Uint8Array`);
}

function exactNonEmptyString(value, label) {
  if (typeof value !== "string" || !value) {
    throw new TypeError(`${label} must be a non-empty string`);
  }
  return value;
}

function normalizeHost(host) {
  return String(host || DEFAULT_HOST).replace(/\/$/, "");
}

function requiredOutcome({ recipient, purchaseUrl, marketplaceUrl }) {
  const issueUrl = String(purchaseUrl);
  const out = {
    authorized: false,
    code: "machine_envelope_required",
    detail: (
      "Present one unexpired Agent Guild machine envelope bound to the exact "
      + "message bytes, this recipient, its purpose and message id. The proof "
      + "is consumed once before consequential work begins."
    ),
    acquisition: {
      issue: issueUrl,
      schema: `${normalizeHost(DEFAULT_HOST)}/envelopes`,
      buyer: `${normalizeHost(DEFAULT_HOST)}/sdk/agentguild_envelope_client.mjs`,
      recipient,
      payload: "RFC 8785 JCS of the A2A message with metadata omitted",
      verification: "local signature + binding checks, then atomic replay consume",
    },
  };
  if (marketplaceUrl) out.acquisition.marketplace = String(marketplaceUrl);
  return out;
}

/** Canonical bytes both A2A sender and receiver commit to. */
export function a2aMessageBytes(message) {
  if (!message || typeof message !== "object" || Array.isArray(message)) {
    throw new TypeError("A2A message must be an object");
  }
  const semanticMessage = structuredClone(message);
  if (semanticMessage.metadata && typeof semanticMessage.metadata === "object") {
    delete semanticMessage.metadata[MACHINE_ENVELOPE_METADATA_KEY];
    if (Object.keys(semanticMessage.metadata).length === 0) {
      delete semanticMessage.metadata;
    }
  }
  exactNonEmptyString(semanticMessage.messageId, "message.messageId");
  if (!Array.isArray(semanticMessage.parts)) {
    throw new TypeError("message.parts must be an array");
  }
  return Buffer.from(canon(semanticMessage), "utf8");
}

/** Context the sender must seal alongside an A2A message commitment. */
export function a2aEnvelopeContext(message) {
  return {
    protocol: A2A_ENVELOPE_CONTEXT_PROTOCOL,
    message_id: exactNonEmptyString(message?.messageId, "message.messageId"),
  };
}

/** Bounded, process-local replay store. Distributed receivers should replace it. */
export function createMemoryEnvelopeReplayStore({
  maxEntries = 10_000,
  now = () => Date.now(),
} = {}) {
  if (!Number.isInteger(maxEntries) || maxEntries < 1) {
    throw new RangeError("maxEntries must be a positive integer");
  }
  const consumed = new Map();
  return Object.freeze({
    async consume({ key, expiresAt }) {
      const currentTime = Number(now());
      if (!Number.isFinite(currentTime)) {
        throw new TypeError("replay-store now() must return epoch milliseconds");
      }
      for (const [candidate, expiry] of consumed) {
        if (expiry <= currentTime) consumed.delete(candidate);
      }
      if (consumed.has(key)) return false;
      const expiry = new Date(expiresAt).getTime();
      if (!Number.isFinite(expiry) || expiry <= currentTime) return false;
      // Never evict an unexpired entry: doing so would make its envelope
      // replayable. Capacity exhaustion fails closed until an entry expires.
      if (consumed.size >= maxEntries) return false;
      consumed.set(key, expiry);
      return true;
    },
  });
}

/** Explicit online verifier for receivers that choose live issuer-key rotation. */
export function createOnlineAgentGuildEnvelopeVerifier({
  host = DEFAULT_HOST,
  fetchImpl = globalThis.fetch,
} = {}) {
  if (typeof fetchImpl !== "function") {
    throw new TypeError("fetchImpl must be a function");
  }
  const base = normalizeHost(host);
  return async (envelope) => {
    const response = await fetchImpl(`${base}/envelopes/verify`, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify({ envelope }),
    });
    if (!response.ok) {
      throw new Error(`Agent Guild envelope verification failed: HTTP ${response.status}`);
    }
    return response.json();
  };
}

/**
 * Build a fail-closed receiver gate.
 *
 * Supply either a non-empty `expectedIssuers` array for offline verification or
 * an explicit `verifyEnvelope` function (for example the online helper above).
 */
export function createAgentGuildMachineEnvelopeReceiver({
  recipient,
  expectedIssuers = null,
  verifyEnvelope = null,
  allowedKinds = DEFAULT_KINDS,
  replayStore = createMemoryEnvelopeReplayStore(),
  purchaseUrl = `${DEFAULT_HOST}/envelopes/issue`,
  marketplaceUrl = null,
  now = () => new Date(),
} = {}) {
  const expectedRecipient = exactNonEmptyString(recipient, "recipient");
  const issuerSet = Array.isArray(expectedIssuers)
    ? new Set(expectedIssuers.map((value) => exactNonEmptyString(value, "issuer")))
    : null;
  if (typeof verifyEnvelope !== "function" && (!issuerSet || issuerSet.size === 0)) {
    throw new TypeError(
      "expectedIssuers must pin at least one Guild DID unless verifyEnvelope is supplied"
    );
  }
  if (!Array.isArray(allowedKinds) || allowedKinds.length === 0) {
    throw new TypeError("allowedKinds must be a non-empty array");
  }
  const kindSet = new Set(allowedKinds.map((value) =>
    exactNonEmptyString(value, "allowed kind").toLowerCase()));
  if (!replayStore || typeof replayStore.consume !== "function") {
    throw new TypeError("replayStore.consume must be an atomic async function");
  }
  const missing = requiredOutcome({
    recipient: expectedRecipient,
    purchaseUrl,
    marketplaceUrl,
  });

  async function authorize({ envelope, payload, requiredContext = null } = {}) {
    if (!envelope || typeof envelope !== "object" || Array.isArray(envelope)) {
      return structuredClone(missing);
    }
    let verification;
    try {
      verification = typeof verifyEnvelope === "function"
        ? await verifyEnvelope(envelope)
        : verifyMachineEnvelope(envelope, { now: now() });
    } catch (error) {
      return {
        authorized: false,
        code: "machine_envelope_verification_unavailable",
        detail: String(error?.message || error),
      };
    }
    if (!verification?.valid) {
      return { authorized: false, code: "machine_envelope_invalid", verification };
    }
    if (issuerSet && !issuerSet.has(verification.issuer)) {
      return { authorized: false, code: "machine_envelope_untrusted_issuer" };
    }
    const message = envelope.message || {};
    if (message.recipient !== expectedRecipient) {
      return { authorized: false, code: "machine_envelope_wrong_recipient" };
    }
    if (!kindSet.has(String(message.kind || "").toLowerCase())) {
      return { authorized: false, code: "machine_envelope_wrong_purpose" };
    }
    const payloadSha256 = sha256Hex(asBytes(payload));
    if (message.payload_sha256 !== payloadSha256) {
      return { authorized: false, code: "machine_envelope_payload_mismatch" };
    }
    if (requiredContext && canon(message.context || {}) !== canon(requiredContext)) {
      return { authorized: false, code: "machine_envelope_context_mismatch" };
    }
    const envelopeId = exactNonEmptyString(envelope.id, "envelope.id");
    const consumed = await replayStore.consume({
      key: envelopeId,
      expiresAt: envelope.valid_until,
    });
    if (!consumed) {
      return { authorized: false, code: "machine_envelope_replayed_or_expired" };
    }
    return {
      authorized: true,
      code: "machine_envelope_authorized",
      senderDid: verification.senderDid || envelope.sender?.did || null,
      envelopeId,
      issuer: verification.issuer,
      kind: message.kind,
      payloadSha256,
      validUntil: envelope.valid_until,
      scope: (
        "authenticated provenance and exact-message authorization only; "
        + "payload truth and policy safety remain receiver decisions"
      ),
    };
  }

  async function authorizeA2AMessage(message) {
    const envelope = message?.metadata?.[MACHINE_ENVELOPE_METADATA_KEY];
    return authorize({
      envelope,
      payload: a2aMessageBytes(message),
      requiredContext: a2aEnvelopeContext(message),
    });
  }

  return Object.freeze({
    authorize,
    authorizeA2AMessage,
    requirement: structuredClone(missing),
  });
}
