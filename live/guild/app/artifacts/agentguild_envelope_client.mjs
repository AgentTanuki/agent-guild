// Agent Guild — one-call buyer for sender-authenticated machine envelopes.
//
// Collapses the machine purchase path into one function invocation:
//   payload bytes -> SHA-256 commitment -> EVM caller proof -> x402 retry
//   -> Guild envelope -> offline signature verification.
//
// This file never reads, stores or transmits a wallet/private key. It accepts
// signer objects. The optional EVM factory uses the official x402 v2 packages;
// install them in the buyer project:
//   npm install @x402/fetch @x402/evm
//
//   import { privateKeyToAccount } from "viem/accounts";
//   import {
//     createEvmMachineEnvelopeClient,
//   } from "./agentguild_envelope_client.mjs";
//
//   const client = await createEvmMachineEnvelopeClient({
//     evmSigner: privateKeyToAccount(process.env.EVM_PRIVATE_KEY),
//   });
//   const result = await client.issue({
//     payload: JSON.stringify({ action: "delegate", task: "42" }),
//     kind: "delegation",
//     recipient: "did:key:z6Mk...",
//   });
//   // result.verification.valid === true; result.envelope is portable.

import {
  createHash,
  createPrivateKey,
  createPublicKey,
  randomBytes,
  sign as edSign,
} from "node:crypto";
import {
  DEFAULT_HOST,
  canon,
  verifyMachineEnvelope,
} from "./agentguild_verify.mjs";

export const CALLER_PROOF_PROTOCOL = "agent-guild/caller-proof/v1";
export const EVM_CALLER_PROOF_PROTOCOL = "agent-guild/caller-proof-evm/v1";
export const MACHINE_ENVELOPE_PROTOCOL = "agent-guild/machine-envelope/v1";
export const CALLER_PROOF_HEADER = "X-Guild-Caller-Proof";
export const BASE_MAINNET_CHAIN_ID = 8453;

const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
const ED25519_PKCS8_PREFIX = Buffer.from("302e020100300506032b657004220420", "hex");
const ED25519_SPKI_PREFIX = Buffer.from("302a300506032b6570032100", "hex");

function bytes(value, field) {
  if (typeof value === "string") return Buffer.from(value, "utf8");
  if (value instanceof ArrayBuffer) return Buffer.from(value);
  if (ArrayBuffer.isView(value)) {
    return Buffer.from(value.buffer, value.byteOffset, value.byteLength);
  }
  throw new TypeError(`${field} must be a string, ArrayBuffer or Uint8Array`);
}

function b58encode(value) {
  const input = Buffer.from(value);
  let n = input.length ? BigInt("0x" + input.toString("hex")) : 0n;
  let out = "";
  while (n > 0n) {
    const r = Number(n % 58n);
    n /= 58n;
    out = B58[r] + out;
  }
  for (const b of input) {
    if (b === 0) out = "1" + out;
    else break;
  }
  return out;
}

function sha256Hex(value) {
  return createHash("sha256").update(value).digest("hex");
}

function signatureHex(value, protocol) {
  if (protocol === EVM_CALLER_PROOF_PROTOCOL) {
    if (typeof value === "string") {
      const hex = value.startsWith("0x") ? value.slice(2) : value;
      if (!/^[0-9a-fA-F]{130}$/.test(hex)) {
        throw new TypeError("EVM signer returned a string that is not a 65-byte signature");
      }
      return "0x" + hex.toLowerCase();
    }
    const out = bytes(value, "signature");
    if (out.length !== 65) throw new TypeError("EVM signature must be 65 bytes");
    return "0x" + out.toString("hex");
  }
  if (typeof value === "string") {
    if (!/^[0-9a-fA-F]{128}$/.test(value)) {
      throw new TypeError("signer returned a string that is not a 64-byte hex signature");
    }
    return value.toLowerCase();
  }
  const out = bytes(value, "signature");
  if (out.length !== 64) throw new TypeError("Ed25519 signature must be 64 bytes");
  return out.toString("hex");
}

function normaliseRawPrivateKey(privateKey) {
  if (typeof privateKey === "string") {
    const hex = privateKey.startsWith("0x") ? privateKey.slice(2) : privateKey;
    if (!/^[0-9a-fA-F]{64}$/.test(hex)) {
      throw new TypeError("Ed25519 private key must be 32 bytes (64 hex characters)");
    }
    return Buffer.from(hex, "hex");
  }
  const raw = bytes(privateKey, "Ed25519 private key");
  if (raw.length !== 32) throw new TypeError("Ed25519 private key must be 32 bytes");
  return raw;
}

/** Build a non-custodial did:key signer from a raw 32-byte Ed25519 key. */
export function didKeySigner(privateKey) {
  const raw = normaliseRawPrivateKey(privateKey);
  const key = createPrivateKey({
    key: Buffer.concat([ED25519_PKCS8_PREFIX, raw]),
    format: "der",
    type: "pkcs8",
  });
  const publicDer = createPublicKey(key).export({ format: "der", type: "spki" });
  if (!Buffer.from(publicDer).subarray(0, ED25519_SPKI_PREFIX.length)
    .equals(ED25519_SPKI_PREFIX)) {
    throw new Error("private key did not derive an Ed25519 public key");
  }
  const publicRaw = Buffer.from(publicDer).subarray(-32);
  const multibase = "z" + b58encode(Buffer.concat([
    Buffer.from([0xed, 0x01]), publicRaw,
  ]));
  const did = "did:key:" + multibase;
  return Object.freeze({
    did,
    verificationMethod: `${did}#${multibase}`,
    async sign(message) {
      return edSign(null, bytes(message, "message"), key);
    },
  });
}

/** Use one caller-owned Base EOA for both exact-body proof and x402 payment. */
export function evmWalletCallerProofSigner(
  evmSigner, { chainId = BASE_MAINNET_CHAIN_ID } = {},
) {
  if (!evmSigner || typeof evmSigner.address !== "string"
      || typeof evmSigner.signMessage !== "function") {
    throw new TypeError("evmSigner must expose address and async signMessage(...)");
  }
  if (Number(chainId) !== BASE_MAINNET_CHAIN_ID) {
    throw new RangeError("the EVM caller-proof v1 identity is Base-mainnet-only");
  }
  const address = evmSigner.address.toLowerCase();
  if (!/^0x[0-9a-f]{40}$/.test(address)) {
    throw new TypeError("evmSigner.address must be a 20-byte EVM address");
  }
  const did = `did:pkh:eip155:${BASE_MAINNET_CHAIN_ID}:${address}`;
  return Object.freeze({
    did,
    verificationMethod: `${did}#blockchainAccountId`,
    callerProofProtocol: EVM_CALLER_PROOF_PROTOCOL,
    async sign(message) {
      return evmSigner.signMessage({ message: { raw: bytes(message, "message") } });
    },
  });
}

function proofNonce() {
  return randomBytes(24).toString("base64url");
}

/** Create the exact-body-bound caller proof required before payment. */
export async function createCallerProof({
  signer,
  method = "POST",
  resource = "/envelopes/issue",
  body,
  ttlSeconds = 300,
  nonce = proofNonce(),
  now = Date.now(),
}) {
  const protocol = signer?.callerProofProtocol || CALLER_PROOF_PROTOCOL;
  const didKey = protocol === CALLER_PROOF_PROTOCOL
    && signer?.did?.startsWith("did:key:");
  const evm = protocol === EVM_CALLER_PROOF_PROTOCOL
    && signer?.did?.startsWith(`did:pkh:eip155:${BASE_MAINNET_CHAIN_ID}:`);
  if ((!didKey && !evm) || typeof signer?.sign !== "function") {
    throw new TypeError("signer must expose a supported DID, proof protocol and async sign(bytes)");
  }
  const ttl = Math.trunc(Number(ttlSeconds));
  if (!Number.isFinite(ttl) || ttl < 1 || ttl > 600) {
    throw new RangeError("caller-proof ttlSeconds must be 1..600");
  }
  if (typeof nonce !== "string" || nonce.length < 8 || nonce.length > 128) {
    throw new RangeError("caller-proof nonce must be 8..128 characters");
  }
  const iat = Math.floor((now instanceof Date ? now.getTime() : Number(now)) / 1000);
  const payload = {
    v: protocol,
    did: signer.did,
    method: String(method),
    resource: String(resource),
    body_sha256: sha256Hex(bytes(body, "body")),
    iat,
    exp: iat + ttl,
    nonce,
    aud: "agent-guild",
  };
  const signature = signatureHex(
    await signer.sign(Buffer.from(canon(payload), "utf8")), protocol,
  );
  return {
    payload,
    signature,
    verificationMethod: signer.verificationMethod
      || (didKey
        ? `${signer.did}#${signer.did.slice("did:key:".length)}`
        : `${signer.did}#blockchainAccountId`),
  };
}

export function encodeCallerProof(proof) {
  return Buffer.from(JSON.stringify(proof), "utf8").toString("base64");
}

function optional(target, key, value) {
  if (value !== undefined && value !== null && value !== "") target[key] = value;
}

function digestInput(value, digest, field) {
  if (value !== undefined && digest !== undefined) {
    throw new TypeError(`provide ${field} bytes or ${field}Sha256, not both`);
  }
  if (digest !== undefined) {
    const out = String(digest).toLowerCase();
    if (!/^[0-9a-f]{64}$/.test(out)) {
      throw new TypeError(`${field}Sha256 must be 64 hexadecimal characters`);
    }
    return out;
  }
  if (value === undefined) throw new TypeError(`${field} is required`);
  return sha256Hex(bytes(value, field));
}

/** Build the privacy-preserving issuance body without sending payload bytes. */
export function machineEnvelopeRequest({
  payload,
  payloadSha256,
  kind = "message",
  recipient,
  nonce = "msg_" + randomBytes(18).toString("base64url"),
  ttlSeconds = 3600,
  payloadMediaType,
  resource,
  replyTo,
  constraints,
  constraintsSha256,
  value,
  context,
}) {
  if (typeof recipient !== "string" || !recipient.trim()) {
    throw new TypeError("recipient is required");
  }
  const request = {
    kind,
    recipient,
    payload_sha256: digestInput(payload, payloadSha256, "payload"),
    nonce,
    ttl_seconds: ttlSeconds,
  };
  optional(request, "payload_media_type", payloadMediaType);
  optional(request, "resource", resource);
  optional(request, "reply_to", replyTo);
  if (constraints !== undefined || constraintsSha256 !== undefined) {
    request.constraints_sha256 = digestInput(
      constraints, constraintsSha256, "constraints",
    );
  }
  optional(request, "value", value);
  optional(request, "context", context);
  return request;
}

export class MachineEnvelopePurchaseError extends Error {
  constructor(message, { status = 0, detail = null } = {}) {
    super(message);
    this.name = "MachineEnvelopePurchaseError";
    this.status = status;
    this.detail = detail;
  }
}

async function responseBody(response) {
  const text = await response.text();
  if (!text) return null;
  try { return JSON.parse(text); } catch { return text; }
}

async function fetchIssuerDid(host, fetchImpl) {
  const url = new URL("/.well-known/agent-guild-did.json", host);
  const response = await fetchImpl(url, { method: "GET" });
  if (!response.ok) {
    throw new MachineEnvelopePurchaseError(
      `cannot pin Agent Guild issuer: HTTP ${response.status}`,
      { status: response.status },
    );
  }
  const document = await response.json();
  if (!document || typeof document.did !== "string"
      || !document.did.startsWith("did:key:")) {
    throw new MachineEnvelopePurchaseError("issuer endpoint did not return a did:key");
  }
  return document.did;
}

/**
 * Buy and verify one machine envelope.
 *
 * `paidFetch` has the standard fetch signature. Pass an official
 * `wrapFetchWithPayment(fetch, x402Client)` result, or use the EVM factory
 * below. The same body and caller proof are retained across the automatic
 * 402 retry; Agent Guild consumes the proof nonce only when execution begins.
 */
export async function issueMachineEnvelope({
  signer,
  paidFetch = globalThis.fetch,
  fetchImpl = globalThis.fetch,
  host = DEFAULT_HOST,
  expectedIssuer,
  pinIssuer = true,
  headers,
  signal,
  ...requestOptions
}) {
  if (typeof paidFetch !== "function" || typeof fetchImpl !== "function") {
    throw new TypeError("paidFetch and fetchImpl must be fetch-compatible functions");
  }
  const endpoint = new URL("/envelopes/issue", host);
  const request = machineEnvelopeRequest(requestOptions);
  const rawBody = JSON.stringify(request);
  const proof = await createCallerProof({
    signer,
    method: "POST",
    resource: endpoint.pathname + endpoint.search,
    body: rawBody,
  });

  // Resolve the signing authority before payment. A broken or substituted
  // issuer endpoint aborts before the wallet can authorize value.
  const issuer = expectedIssuer || (pinIssuer
    ? await fetchIssuerDid(host, fetchImpl)
    : null);

  const requestHeaders = new Headers(headers || {});
  requestHeaders.set("content-type", "application/json");
  requestHeaders.set(CALLER_PROOF_HEADER, encodeCallerProof(proof));
  const response = await paidFetch(endpoint, {
    method: "POST",
    headers: requestHeaders,
    body: rawBody,
    signal,
  });
  const body = await responseBody(response);
  if (!response.ok) {
    const missingPaymentClient = response.status === 402
      ? " Configure the official @x402/fetch client or use createEvmMachineEnvelopeClient()."
      : "";
    throw new MachineEnvelopePurchaseError(
      `machine envelope issuance failed: HTTP ${response.status}.${missingPaymentClient}`,
      { status: response.status, detail: body },
    );
  }
  const verification = verifyMachineEnvelope(body, { expectedIssuer: issuer });
  if (!verification.valid) {
    throw new MachineEnvelopePurchaseError(
      "paid response was not a valid, pinned Agent Guild machine envelope",
      { status: response.status, detail: {
        verification,
        envelope: body,
        paymentResponse: response.headers.get("PAYMENT-RESPONSE"),
      } },
    );
  }
  return {
    envelope: body,
    verification,
    paymentResponse: response.headers.get("PAYMENT-RESPONSE"),
  };
}

/**
 * Configure the official x402 v2 EVM client and return a one-method buyer.
 * The caller supplies signer objects; this module never handles wallet files.
 */
export async function createEvmMachineEnvelopeClient({
  didSigner,
  evmSigner,
  evmChainId = BASE_MAINNET_CHAIN_ID,
  fetchImpl = globalThis.fetch,
  host = DEFAULT_HOST,
  expectedIssuer,
  pinIssuer = true,
}) {
  if (!evmSigner) throw new TypeError("evmSigner is required");
  const callerSigner = didSigner
    || evmWalletCallerProofSigner(evmSigner, { chainId: evmChainId });
  let x402Fetch;
  let x402Evm;
  try {
    [x402Fetch, x402Evm] = await Promise.all([
      import("@x402/fetch"),
      import("@x402/evm/exact/client"),
    ]);
  } catch (error) {
    throw new MachineEnvelopePurchaseError(
      "install @x402/fetch and @x402/evm to enable automatic Base USDC payment",
      { detail: String(error?.message || error) },
    );
  }
  const x402Client = new x402Fetch.x402Client();
  x402Evm.registerExactEvmScheme(x402Client, { signer: evmSigner });
  const paidFetch = x402Fetch.wrapFetchWithPayment(fetchImpl, x402Client);
  return Object.freeze({
    async issue(options) {
      return issueMachineEnvelope({
        ...options,
        signer: callerSigner,
        paidFetch,
        fetchImpl,
        host,
        expectedIssuer,
        pinIssuer,
      });
    },
  });
}
