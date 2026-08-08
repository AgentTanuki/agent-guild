#!/usr/bin/env node
// Register or refresh Agent Guild's canonical PayanAgent relay offer.
//
// The seller credential is recovered into a mode-600 temporary file, loaded
// into memory, and deleted immediately. It is never printed, passed in a
// command argument, or written to this repository. Re-registering the same
// externalUrl is idempotent in PayanAgent, so this script is safe to rerun
// after a deployment or price change.

import {
  chmodSync,
  closeSync,
  mkdtempSync,
  openSync,
  readFileSync,
  rmdirSync,
  unlinkSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

const PAYAN = "https://payanagent.com";
const GUILD = "https://agent-guild-5d5r.onrender.com";
const EXTERNAL_URL = `${GUILD}/envelopes/issue`;
const SELLER_ID = "j5745s1y3cy11gbz8592yagyn18c1b12";
const KEYCHAIN_ACCOUNT = "agent-guild-treasury-relay-v2";
const KEYCHAIN_SERVICE = "com.openai.codex.agent-guild.payanagent-treasury-v2";
const KEYCHAIN_BRIDGE = process.env.AG_KEYCHAIN_BRIDGE || join(
  homedir(), "Library", "Application Support", "Agent Guild", "keychain-bridge",
);
const TITLE = "Cryptographically Signed Machine Envelope — Agent Guild";

function recoverCredential() {
  const dir = mkdtempSync(join(tmpdir(), "ag-payan-relay-credential-"));
  chmodSync(dir, 0o700);
  const file = join(dir, "credential.json");
  const fd = openSync(file, "wx", 0o600);
  try {
    const found = spawnSync(
      KEYCHAIN_BRIDGE,
      ["read", KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE],
      { stdio: ["ignore", fd, "ignore"] },
    );
    closeSync(fd);
    if (found.status !== 0) {
      throw new Error("treasury relay credential is unavailable in Keychain");
    }
    const credential = JSON.parse(readFileSync(file, "utf8").trim());
    unlinkSync(file);
    rmdirSync(dir);
    if (credential.agent_id !== SELLER_ID || !credential.api_key) {
      throw new Error("Keychain credential does not belong to the treasury relay seller");
    }
    return credential;
  } catch (error) {
    try { closeSync(fd); } catch {}
    try { unlinkSync(file); } catch {}
    try { rmdirSync(dir); } catch {}
    throw error;
  }
}

async function jsonRequest(path, apiKey, { method = "GET", body } = {}) {
  const response = await fetch(`${PAYAN}${path}`, {
    method,
    headers: {
      accept: "application/json",
      "content-type": "application/json",
      authorization: `Bearer ${apiKey}`,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let parsed;
  try { parsed = text ? JSON.parse(text) : null; } catch { parsed = null; }
  if (!response.ok) {
    const publicReason = parsed?.error || `HTTP ${response.status}`;
    throw new Error(`PayanAgent ${method} ${path} failed: ${publicReason}`);
  }
  return parsed;
}

function inputSchema(buyUrl) {
  return JSON.stringify({
    $schema: "https://json-schema.org/draft/2020-12/schema",
    title: "Agent Guild PayanAgent machine-envelope buy input",
    type: "object",
    additionalProperties: false,
    required: ["request", "caller_proof"],
    properties: {
      request: {
        type: "object",
        additionalProperties: false,
        required: [
          "kind", "recipient", "payload_sha256", "nonce", "ttl_seconds",
          "x402_resource_url",
        ],
        properties: {
          kind: {
            type: "string",
            enum: [
              "message", "intent", "offer", "acceptance", "delegation",
              "authorization", "delivery", "receipt", "revocation",
            ],
          },
          recipient: { type: "string", minLength: 1, maxLength: 512 },
          payload_sha256: { type: "string", pattern: "^[0-9a-fA-F]{64}$" },
          nonce: { type: "string", minLength: 8, maxLength: 128 },
          ttl_seconds: { type: "integer", minimum: 60, maximum: 604800 },
          x402_resource_url: { type: "string", const: buyUrl },
          payload_media_type: { type: "string" },
          resource: { type: "string" },
          reply_to: { type: "string" },
          constraints_sha256: {
            type: "string", pattern: "^[0-9a-fA-F]{64}$",
          },
          value: { type: "object" },
          context: { type: "object" },
        },
      },
      caller_proof: {
        type: "object",
        additionalProperties: false,
        required: ["payload", "signature", "verificationMethod"],
        properties: {
          payload: { type: "object" },
          signature: { type: "string" },
          verificationMethod: { type: "string" },
        },
      },
    },
    description: (
      "Generate this object with Agent Guild's "
      + "machineEnvelopeMarketplaceInput({signer, payanOfferId, ...}). "
      + "The proof signs RFC 8785 JCS(request), including this exact buy URL."
    ),
  });
}

function outputSchema() {
  return JSON.stringify({
    $schema: "https://json-schema.org/draft/2020-12/schema",
    type: "object",
    required: [
      "type", "id", "protocol", "issuer", "issued_at", "valid_until",
      "sender", "message", "proof", "envelope_sha256",
    ],
    properties: {
      type: { const: "AgentGuildMachineEnvelope" },
      protocol: { const: "agent-guild/machine-envelope/v1" },
      sender: { type: "object" },
      message: { type: "object" },
      proof: { type: "string" },
      envelope_sha256: { type: "string", pattern: "^[0-9a-f]{64}$" },
    },
    description: (
      "Guild-signed, offline-verifiable integrity/provenance envelope. It "
      + "attests authenticated sender, exact payload digest and issuance time; "
      + "it does not assert payload truth, recipient acceptance or settlement."
    ),
  });
}

async function main() {
  const credential = recoverCredential();
  if (process.argv.includes("--check")) {
    const sampleOfferId = "kh_payan_relay_readiness_0001";
    JSON.parse(inputSchema(`${PAYAN}/x402/${sampleOfferId}`));
    JSON.parse(outputSchema());
    process.stdout.write(JSON.stringify({
      ready: true,
      sellerId: credential.agent_id,
      externalUrl: EXTERNAL_URL,
      credentialSource: "macOS Keychain",
    }) + "\n");
    return;
  }
  const registration = await jsonRequest("/api/v1/offers", credential.api_key, {
    method: "POST",
    body: {
      title: TITLE,
      description: (
        "Seal a sender-authenticated machine message, intent, offer, "
        + "delegation, authorization, delivery or receipt as an Agent Guild "
        + "signed envelope. The confidential payload stays with the machines; "
        + "only its SHA-256 commitment is sent. Caller proof supports did:key "
        + "or a Base EOA and is nonce-replay protected. Verification is free "
        + "and works offline."
      ),
      category: "Security",
      tags: [
        "machine-envelope", "signed-message", "agent-to-agent", "x402",
        "did", "cryptographic-proof", "non-repudiation", "agent-guild",
      ],
      offerType: "api",
      externalUrl: EXTERNAL_URL,
      httpMethod: "POST",
      inputSchema: JSON.stringify({
        type: "object",
        required: ["request", "caller_proof"],
        description: (
          "After registration, use the exact offer buyUrl as "
          + "request.x402_resource_url. See GET " + GUILD + "/envelopes."
        ),
      }),
      outputSchema: outputSchema(),
      estimatedDurationSeconds: 5,
      previewDescription: (
        "Returns a portable AgentGuildMachineEnvelope signed by the Guild. "
        + "Payload content is never uploaded; free verification is at "
        + GUILD + "/envelopes/verify."
      ),
    },
  });
  const offerId = String(registration?.offerId || "");
  if (!offerId) throw new Error("PayanAgent response did not contain offerId");
  const buyUrl = `${PAYAN}/x402/${offerId}`;

  await jsonRequest(`/api/v1/offers/${offerId}`, credential.api_key, {
    method: "PATCH",
    body: {
      inputSchema: inputSchema(buyUrl),
      outputSchema: outputSchema(),
    },
  });

  process.stdout.write(JSON.stringify({
    registered: true,
    offerId,
    buyUrl,
    offerUrl: `${PAYAN}/marketplace/offers/${offerId}`,
    externalUrl: EXTERNAL_URL,
  }) + "\n");
}

main().catch((error) => {
  process.stderr.write(String(error?.message || error) + "\n");
  process.exit(1);
});
