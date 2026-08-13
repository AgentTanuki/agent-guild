// sdk/integrations/machine_envelope_receiver.entry.mjs
import { createHash } from "node:crypto";

// sdk/agentguild_verify.mjs
import { createPublicKey, verify as edVerify } from "node:crypto";
import { createHash as _createHash } from "node:crypto";
var DEFAULT_HOST = "https://agent-guild-5d5r.onrender.com";
var B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function b58decode(s) {
  let n = 0n;
  for (const ch of s) {
    const i = B58.indexOf(ch);
    if (i < 0) throw new Error("bad base58");
    n = n * 58n + BigInt(i);
  }
  let hex = n.toString(16);
  if (hex.length % 2) hex = "0" + hex;
  let bytes = hex === "0" ? [] : [...Buffer.from(hex, "hex")];
  for (const ch of s) {
    if (ch === "1") bytes.unshift(0);
    else break;
  }
  return Buffer.from(bytes);
}
function publicKeyFromDid(did) {
  const mb = did.startsWith("did:key:") ? did.slice(8) : did;
  if (!mb.startsWith("z")) throw new Error("unsupported did multibase");
  const raw = b58decode(mb.slice(1));
  if (raw[0] !== 237 || raw[1] !== 1) throw new Error("did:key is not Ed25519");
  return raw.subarray(2);
}
function edKey(raw32) {
  const der = Buffer.concat([Buffer.from("302a300506032b6570032100", "hex"), raw32]);
  return createPublicKey({ key: der, format: "der", type: "spki" });
}
function canon(v) {
  if (v === null) return "null";
  if (Array.isArray(v)) return "[" + v.map(canon).join(",") + "]";
  const t = typeof v;
  if (t === "object") {
    return "{" + Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + canon(v[k])).join(",") + "}";
  }
  if (t === "number") {
    if (!Number.isFinite(v)) throw new Error("NaN/Infinity not permitted");
    return JSON.stringify(v);
  }
  return JSON.stringify(v);
}
function verifySig(payload, sigHex, raw32) {
  try {
    return edVerify(
      null,
      Buffer.from(canon(payload), "utf8"),
      edKey(raw32),
      Buffer.from(sigHex, "hex")
    );
  } catch {
    return false;
  }
}
function multibaseB58Decode(s) {
  if (!s.startsWith("z")) throw new Error("not base58btc multibase");
  return b58decode(s.slice(1));
}
function verifyDataIntegrity(vc) {
  const proof = vc.proof || {};
  if (proof.cryptosuite !== "eddsa-jcs-2022" || !proof.proofValue) return false;
  const { proofValue, ...proofConfig } = proof;
  const { proof: _omit, ...document } = vc;
  if ("@context" in proofConfig && canon(proofConfig["@context"]) !== canon(document["@context"] ?? null)) return false;
  const vm = proof.verificationMethod || "";
  const did = vm ? vm.split("#", 1)[0] : vc.issuer || "";
  if (vc.issuer && did !== vc.issuer) return false;
  const { createHash: createHash2 } = awaitlessCrypto();
  const hashData = Buffer.concat([
    createHash2("sha256").update(Buffer.from(canon(proofConfig), "utf8")).digest(),
    createHash2("sha256").update(Buffer.from(canon(document), "utf8")).digest()
  ]);
  try {
    return edVerify(
      null,
      hashData,
      edKey(publicKeyFromDid(did)),
      multibaseB58Decode(proofValue)
    );
  } catch {
    return false;
  }
}
function awaitlessCrypto() {
  return { createHash: _createHash };
}
function verifyCredential(vc) {
  try {
    const proof = vc.proof || {};
    if (proof.type === "DataIntegrityProof") return verifyDataIntegrity(vc);
    if (!proof.proofValue) return false;
    const { proofValue, ...proofRest } = proof;
    const { proof: _omit, ...rest } = vc;
    const payload = { ...rest, proof: proofRest };
    return verifySig(payload, proofValue, publicKeyFromDid(vc.issuer));
  } catch {
    return false;
  }
}
function verifyCheckpoint(cp) {
  try {
    if (typeof cp.proof !== "string") return false;
    const { proof, ...body } = cp;
    return verifySig(body, proof, publicKeyFromDid(cp.issuer));
  } catch {
    return false;
  }
}
function verifyPassport(vc, { expectedIssuer = null } = {}) {
  const valid = verifyCredential(vc);
  const subj = vc && vc.credentialSubject || {};
  const issuer = vc && vc.issuer || "";
  const anchor = valid ? subj.ledger_anchor || {} : {};
  const cp = anchor.checkpoint || null;
  const { id, ...claims } = subj;
  return {
    valid,
    issuer,
    issuerMatches: expectedIssuer ? issuer === expectedIssuer : null,
    subject: subj.id || "",
    claims: valid ? claims : null,
    verifiableCollaborations: anchor.verifiable_collaborations ?? null,
    checkpointValid: cp ? verifyCheckpoint(cp) : null
  };
}
function verifyMachineEnvelope(envelope, { expectedIssuer = null, now = /* @__PURE__ */ new Date() } = {}) {
  try {
    if (envelope.type !== "AgentGuildMachineEnvelope" || envelope.protocol !== "agent-guild/machine-envelope/v1") {
      throw new Error("unsupported envelope");
    }
    const issuer = envelope.issuer || "";
    const { envelope_sha256: claimedDigest, ...withoutDigest } = envelope;
    const { proof, ...signed } = withoutDigest;
    if (typeof proof !== "string" || typeof claimedDigest !== "string") {
      throw new Error("missing proof/digest");
    }
    const digestValid = _createHash("sha256").update(Buffer.from(canon(withoutDigest), "utf8")).digest("hex") === claimedDigest;
    const signatureValid = verifySig(signed, proof, publicKeyFromDid(issuer));
    const validUntil = new Date(envelope.valid_until);
    const expired = !Number.isFinite(validUntil.getTime()) || now > validUntil;
    const issuerMatches = expectedIssuer ? issuer === expectedIssuer : null;
    return {
      valid: signatureValid && digestValid && !expired && issuerMatches !== false,
      signatureValid,
      digestValid,
      expired,
      issuer,
      issuerMatches,
      senderDid: envelope.sender?.did ?? null,
      recipient: envelope.message?.recipient ?? null,
      payloadSha256: envelope.message?.payload_sha256 ?? null,
      note: "Integrity/provenance only; this does not attest payload truth, recipient acceptance or settlement."
    };
  } catch {
    return {
      valid: false,
      signatureValid: false,
      digestValid: false,
      expired: true,
      issuer: envelope?.issuer ?? ""
    };
  }
}
async function getJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status} for ${url}`);
  return r.json();
}
async function fetchPassport(agentId, host = DEFAULT_HOST) {
  return getJson(`${host}/agents/${agentId}/passport`);
}
async function issuerDid(host = DEFAULT_HOST) {
  return (await getJson(`${host}/.well-known/agent-guild-did.json`)).did;
}
async function vet(agentId, host = DEFAULT_HOST, { pinIssuer = true } = {}) {
  const vc = await fetchPassport(agentId, host);
  const expected = pinIssuer ? await issuerDid(host) : null;
  const res = verifyPassport(vc, { expectedIssuer: expected });
  const claims = res.claims || {};
  const trustworthy = res.valid && (res.issuerMatches === true || res.issuerMatches === null);
  return {
    agentId,
    verified: trustworthy,
    recommendation: trustworthy ? claims.recommendation ?? null : null,
    trust: claims.trust ?? null,
    risk: claims.risk ?? null,
    verifiableCollaborations: res.verifiableCollaborations,
    issuer: res.issuer,
    raw: res
  };
}
if (import.meta.url === `file://${process.argv[1]}`) {
  const [, , agentId, host] = process.argv;
  if (!agentId) {
    console.error("usage: node agentguild_verify.mjs <agent_id> [host]");
    process.exit(2);
  }
  vet(agentId, host || DEFAULT_HOST).then((d) => console.log(JSON.stringify(d, null, 2)));
}

// sdk/integrations/machine_envelope_receiver.entry.mjs
var MACHINE_ENVELOPE_METADATA_KEY = "io.agent-guild/machine-envelope";
var A2A_ENVELOPE_CONTEXT_PROTOCOL = "agent-guild/a2a-message-binding/v1";
var A2A_MACHINE_ENVELOPE_EXTENSION_URI = `${DEFAULT_HOST}/extensions/machine-envelope/v1`;
var DEFAULT_KINDS = Object.freeze([
  "intent",
  "delegation",
  "authorization",
  "acceptance",
  "delivery",
  "payment"
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
    detail: "Present one unexpired Agent Guild machine envelope bound to the exact message bytes, this recipient, its purpose and message id. The proof is consumed once before consequential work begins.",
    acquisition: {
      issue: issueUrl,
      schema: `${normalizeHost(DEFAULT_HOST)}/envelopes`,
      buyer: `${normalizeHost(DEFAULT_HOST)}/sdk/agentguild_envelope_client.mjs`,
      recipient,
      payload: "RFC 8785 JCS of the A2A message with only the envelope metadata entry omitted",
      verification: "local signature + binding checks, then atomic replay consume"
    }
  };
  if (marketplaceUrl) out.acquisition.marketplace = String(marketplaceUrl);
  return out;
}
function a2aMessageBytes(message) {
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
function a2aEnvelopeContext(message) {
  return {
    protocol: A2A_ENVELOPE_CONTEXT_PROTOCOL,
    message_id: exactNonEmptyString(message?.messageId, "message.messageId")
  };
}
function headerValue(source, name) {
  const headers = source?.headers || source;
  if (headers && typeof headers.get === "function") {
    return headers.get(name) || "";
  }
  if (!headers || typeof headers !== "object" || Array.isArray(headers)) {
    return "";
  }
  const match = Object.entries(headers).find(
    ([key]) => key.toLowerCase() === name.toLowerCase()
  );
  const value = match?.[1];
  return Array.isArray(value) ? value.join(",") : String(value || "");
}
function a2aExtensionActivated(requestOrHeaders, extensionUri = A2A_MACHINE_ENVELOPE_EXTENSION_URI) {
  const expected = exactNonEmptyString(extensionUri, "extensionUri");
  return headerValue(requestOrHeaders, "A2A-Extensions").split(",").map((value) => value.trim()).filter(Boolean).includes(expected);
}
function createMemoryEnvelopeReplayStore({
  maxEntries = 1e4,
  now = () => Date.now()
} = {}) {
  if (!Number.isInteger(maxEntries) || maxEntries < 1) {
    throw new RangeError("maxEntries must be a positive integer");
  }
  const consumed = /* @__PURE__ */ new Map();
  return Object.freeze({
    async consume({ key, expiresAt }) {
      const currentTime = Number(now());
      if (!Number.isFinite(currentTime)) {
        throw new TypeError("replay-store now() must return epoch milliseconds");
      }
      for (const [candidate, expiry2] of consumed) {
        if (expiry2 <= currentTime) consumed.delete(candidate);
      }
      if (consumed.has(key)) return false;
      const expiry = new Date(expiresAt).getTime();
      if (!Number.isFinite(expiry) || expiry <= currentTime) return false;
      if (consumed.size >= maxEntries) return false;
      consumed.set(key, expiry);
      return true;
    }
  });
}
function createOnlineAgentGuildEnvelopeVerifier({
  host = DEFAULT_HOST,
  fetchImpl = globalThis.fetch
} = {}) {
  if (typeof fetchImpl !== "function") {
    throw new TypeError("fetchImpl must be a function");
  }
  const base = normalizeHost(host);
  return async (envelope) => {
    const response = await fetchImpl(`${base}/envelopes/verify`, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify({ envelope })
    });
    if (!response.ok) {
      throw new Error(`Agent Guild envelope verification failed: HTTP ${response.status}`);
    }
    return response.json();
  };
}
function createAgentGuildMachineEnvelopeReceiver({
  recipient,
  expectedIssuers = null,
  verifyEnvelope = null,
  allowedKinds = DEFAULT_KINDS,
  replayStore = createMemoryEnvelopeReplayStore(),
  purchaseUrl = `${DEFAULT_HOST}/envelopes/issue`,
  marketplaceUrl = null,
  now = () => /* @__PURE__ */ new Date()
} = {}) {
  const expectedRecipient = exactNonEmptyString(recipient, "recipient");
  const issuerSet = Array.isArray(expectedIssuers) ? new Set(expectedIssuers.map((value) => exactNonEmptyString(value, "issuer"))) : null;
  if (typeof verifyEnvelope !== "function" && (!issuerSet || issuerSet.size === 0)) {
    throw new TypeError(
      "expectedIssuers must pin at least one Guild DID unless verifyEnvelope is supplied"
    );
  }
  if (!Array.isArray(allowedKinds) || allowedKinds.length === 0) {
    throw new TypeError("allowedKinds must be a non-empty array");
  }
  const kindSet = new Set(allowedKinds.map((value) => exactNonEmptyString(value, "allowed kind").toLowerCase()));
  if (!replayStore || typeof replayStore.consume !== "function") {
    throw new TypeError("replayStore.consume must be an atomic async function");
  }
  const missing = requiredOutcome({
    recipient: expectedRecipient,
    purchaseUrl,
    marketplaceUrl
  });
  async function authorize({ envelope, payload, requiredContext = null } = {}) {
    if (!envelope || typeof envelope !== "object" || Array.isArray(envelope)) {
      return structuredClone(missing);
    }
    let verification;
    try {
      verification = typeof verifyEnvelope === "function" ? await verifyEnvelope(envelope) : verifyMachineEnvelope(envelope, { now: now() });
    } catch (error) {
      return {
        authorized: false,
        code: "machine_envelope_verification_unavailable",
        detail: String(error?.message || error)
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
      expiresAt: envelope.valid_until
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
      scope: "authenticated provenance and exact-message authorization only; payload truth and policy safety remain receiver decisions"
    };
  }
  async function authorizeA2AMessage(message) {
    const envelope = message?.metadata?.[MACHINE_ENVELOPE_METADATA_KEY];
    return authorize({
      envelope,
      payload: a2aMessageBytes(message),
      requiredContext: a2aEnvelopeContext(message)
    });
  }
  return Object.freeze({
    authorize,
    authorizeA2AMessage,
    requirement: structuredClone(missing)
  });
}
function createAgentGuildA2AEnvelopeExtension({
  receiver,
  acquisitionUrl,
  extensionUri = A2A_MACHINE_ENVELOPE_EXTENSION_URI,
  required = true,
  kind = "offer"
} = {}) {
  if (!receiver || typeof receiver.authorizeA2AMessage !== "function") {
    throw new TypeError("receiver.authorizeA2AMessage must be a function");
  }
  const uri = exactNonEmptyString(extensionUri, "extensionUri");
  const acquisition = exactNonEmptyString(acquisitionUrl, "acquisitionUrl");
  const purpose = exactNonEmptyString(kind, "kind");
  const recipient = exactNonEmptyString(
    receiver.requirement?.acquisition?.recipient,
    "receiver recipient"
  );
  const responseHeaders = Object.freeze({ "A2A-Extensions": uri });
  const card = Object.freeze({
    uri,
    description: "A signed exact-message provenance envelope is required before consequential actions.",
    required: Boolean(required),
    params: Object.freeze({
      metadata_key: MACHINE_ENVELOPE_METADATA_KEY,
      recipient,
      kind: purpose,
      context_protocol: A2A_ENVELOPE_CONTEXT_PROTOCOL,
      acquisition,
      activation_header: "A2A-Extensions",
      message_field_role: "optional descriptive declaration; signed when present",
      free_discovery: true
    })
  });
  async function authorizeA2ARequest({ request = null, headers = null, message } = {}) {
    if (!a2aExtensionActivated(headers || request, uri)) {
      return {
        authorized: false,
        activated: false,
        code: "machine_envelope_extension_required",
        extension: { uri, acquisition },
        responseHeaders: {},
        retry: "Send the identical Message with the exact extension URI in the A2A-Extensions header, then attach its exact-message envelope."
      };
    }
    const result = await receiver.authorizeA2AMessage(message);
    return {
      ...result,
      activated: true,
      extension: uri,
      responseHeaders
    };
  }
  return Object.freeze({
    uri,
    agentCardExtension: card,
    authorizeA2ARequest,
    activated: (requestOrHeaders) => a2aExtensionActivated(requestOrHeaders, uri)
  });
}
export {
  A2A_ENVELOPE_CONTEXT_PROTOCOL,
  A2A_MACHINE_ENVELOPE_EXTENSION_URI,
  MACHINE_ENVELOPE_METADATA_KEY,
  a2aEnvelopeContext,
  a2aExtensionActivated,
  a2aMessageBytes,
  createAgentGuildA2AEnvelopeExtension,
  createAgentGuildMachineEnvelopeReceiver,
  createMemoryEnvelopeReplayStore,
  createOnlineAgentGuildEnvelopeVerifier
};
