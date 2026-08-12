#!/usr/bin/env node
// Idempotently register Agent Guild's caller-bound AGPD-1 Payan relay.
import {
  chmodSync, closeSync, mkdtempSync, openSync, readFileSync, rmdirSync,
  unlinkSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

const PAYAN = "https://payanagent.com";
const GUILD = "https://agent-guild-5d5r.onrender.com";
const EXTERNAL_URL = `${GUILD}/wallet-binding/decision`;
const SELLER_ID = "j5745s1y3cy11gbz8592yagyn18c1b12";
const KEYCHAIN_ACCOUNT = "agent-guild-treasury-relay-v2";
const KEYCHAIN_SERVICE = "com.openai.codex.agent-guild.payanagent-treasury-v2";
const KEYCHAIN_BRIDGE = process.env.AG_KEYCHAIN_BRIDGE || join(
  homedir(), "Library", "Application Support", "Agent Guild", "keychain-bridge",
);

function recoverCredential() {
  const dir = mkdtempSync(join(tmpdir(), "ag-payan-payment-credential-"));
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
    if (found.status !== 0) throw new Error("treasury credential is unavailable");
    const credential = JSON.parse(readFileSync(file, "utf8").trim());
    unlinkSync(file);
    rmdirSync(dir);
    if (credential.agent_id !== SELLER_ID || !credential.api_key) {
      throw new Error("credential does not belong to the treasury seller");
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
    throw new Error(parsed?.error || `PayanAgent HTTP ${response.status}`);
  }
  return parsed;
}

function inputSchema(buyUrl) {
  return JSON.stringify({
    $schema: "https://json-schema.org/draft/2020-12/schema",
    title: "Agent Guild exact-payment AGPD-1 decision buy input",
    type: "object",
    additionalProperties: false,
    required: ["request", "caller_proof"],
    properties: {
      request: {
        type: "object",
        additionalProperties: false,
        required: ["payment", "ttl_seconds", "x402_resource_url"],
        properties: {
          payment: {
            type: "object",
            additionalProperties: false,
            required: ["scheme", "network", "asset", "amount", "pay_to", "resource"],
            properties: {
              scheme: { type: "string", minLength: 1, maxLength: 64 },
              network: { type: "string", const: "eip155:8453" },
              asset: { type: "string", pattern: "^0x[0-9a-fA-F]{40}$" },
              amount: { type: "string", pattern: "^[1-9][0-9]*$", maxLength: 78 },
              pay_to: { type: "string", pattern: "^0x[0-9a-fA-F]{40}$" },
              resource: { type: "string", format: "uri", maxLength: 2048 },
            },
          },
          capability: { type: "string", maxLength: 128 },
          policy: {
            type: "object",
            additionalProperties: false,
            properties: {
              max_risk: { type: "number", minimum: 0, maximum: 100 },
              min_confidence: { type: "number", minimum: 0, maximum: 1 },
            },
          },
          ttl_seconds: { type: "integer", minimum: 60, maximum: 3600 },
          x402_resource_url: { type: "string", const: buyUrl },
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
      "Generate with paymentDecisionMarketplaceInput({signer, payanOfferId, "
      + "payment}). The proof signs RFC 8785 JCS(request), including every "
      + "payment field, policy threshold and this exact buy URL."
    ),
  });
}

function outputSchema() {
  return JSON.stringify({
    $schema: "https://json-schema.org/draft/2020-12/schema",
    type: "object",
    required: ["type", "issuer", "validFrom", "validUntil", "credentialSubject", "proof"],
    properties: {
      type: { type: "array", contains: { const: "AgentGuildPaymentDecision" } },
      issuer: { type: "string", pattern: "^did:key:" },
      validFrom: { type: "string", format: "date-time" },
      validUntil: { type: "string", format: "date-time" },
      credentialSubject: {
        type: "object",
        required: ["contract", "payment", "policy", "decision", "reason"],
        properties: {
          contract: { const: "AGPD-1/1.0" },
          payment: { type: "object" },
          policy: { type: "object" },
          decision: { enum: ["allow", "block"] },
          reason: { type: "string" },
        },
      },
      proof: {
        type: "object",
        properties: { cryptosuite: { const: "eddsa-jcs-2022" } },
        required: ["cryptosuite", "proofValue"],
      },
    },
  });
}

async function main() {
  const credential = recoverCredential();
  if (process.argv.includes("--check")) {
    JSON.parse(inputSchema(`${PAYAN}/x402/kh_payment_decision_check_0001`));
    JSON.parse(outputSchema());
    process.stdout.write(JSON.stringify({
      ready: true, sellerId: credential.agent_id, externalUrl: EXTERNAL_URL,
      credentialSource: "macOS Keychain",
    }) + "\n");
    return;
  }
  const registration = await jsonRequest("/api/v1/offers", credential.api_key, {
    method: "POST",
    body: {
      title: "Pre-Payment Wallet Safety Decision (AGPD-1) — Agent Guild",
      description: (
        "Before an autonomous wallet pays, obtain one short-lived, offline-"
        + "verifiable allow/block credential for the exact payee, Base chain, "
        + "token, atomic amount and resource. It resolves the exact wallet to "
        + "a registered agent, applies current risk/confidence thresholds and "
        + "seals the result with eddsa-jcs-2022. Unknown wallets fail closed "
        + "without being mislabeled as misconduct."
      ),
      category: "Security",
      tags: [
        "wallet-security", "payment-policy", "x402", "agent-payments",
        "AGPD-1", "signed-proof", "counterparty-risk", "agent-guild",
      ],
      offerType: "api",
      externalUrl: EXTERNAL_URL,
      httpMethod: "POST",
      inputSchema: JSON.stringify({
        type: "object", required: ["request", "caller_proof"],
        description: "The exact Payan buy URL is required after registration.",
      }),
      outputSchema: outputSchema(),
      estimatedDurationSeconds: 3,
      previewDescription: (
        "Returns a portable AgentGuildPaymentDecision (AGPD-1/1.0), signed "
        + "and bound to the exact payment selected by the buyer."
      ),
    },
  });
  const offerId = String(registration?.offerId || "");
  if (!offerId) throw new Error("PayanAgent response did not contain offerId");
  const buyUrl = `${PAYAN}/x402/${offerId}`;
  await jsonRequest(`/api/v1/offers/${offerId}`, credential.api_key, {
    method: "PATCH",
    body: { inputSchema: inputSchema(buyUrl), outputSchema: outputSchema() },
  });
  process.stdout.write(JSON.stringify({
    registered: true, offerId, buyUrl,
    offerUrl: `${PAYAN}/marketplace/offers/${offerId}`,
    externalUrl: EXTERNAL_URL,
  }) + "\n");
}

main().catch((error) => {
  process.stderr.write(String(error?.message || error) + "\n");
  process.exit(1);
});
