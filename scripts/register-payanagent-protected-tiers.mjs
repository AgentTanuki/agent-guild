#!/usr/bin/env node
// Idempotently register Agent Guild's exact-notional protected-payment tiers.
import {
  chmodSync, closeSync, mkdtempSync, openSync, readFileSync, rmdirSync,
  unlinkSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

const PAYAN = "https://payanagent.com";
const GUILD = "https://agent-guild-5d5r.onrender.com";
const SELLER_ID = "j5745s1y3cy11gbz8592yagyn18c1b12";
const KEYCHAIN_ACCOUNT = "agent-guild-treasury-relay-v2";
const KEYCHAIN_SERVICE = "com.openai.codex.agent-guild.payanagent-treasury-v2";
const KEYCHAIN_BRIDGE = process.env.AG_KEYCHAIN_BRIDGE || join(
  homedir(), "Library", "Application Support", "Agent Guild", "keychain-bridge",
);
const TIERS = [
  { id: "1000-usdc", notional: "1000", amount: "1000000000", fee: "$2.50" },
  { id: "10000-usdc", notional: "10,000", amount: "10000000000", fee: "$25" },
  { id: "100000-usdc", notional: "100,000", amount: "100000000000", fee: "$250" },
  { id: "1000000-usdc", notional: "1,000,000", amount: "1000000000000", fee: "$2,500" },
  { id: "4000000-usdc", notional: "4,000,000", amount: "4000000000000", fee: "$10,000" },
];

function recoverCredential() {
  const dir = mkdtempSync(join(tmpdir(), "ag-payan-protected-credential-"));
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

function inputSchema(tier, buyUrl) {
  return JSON.stringify({
    $schema: "https://json-schema.org/draft/2020-12/schema",
    title: `Agent Guild protected ${tier.notional} USDC payment decision input`,
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
              scheme: { const: "exact" },
              network: { const: "eip155:8453" },
              asset: {
                type: "string",
                pattern: "^0x833589[fF][cC][dD]6[eE][dD][bB]6[eE]08[fF]4[cC]7[cC]32[dD]4[fF]71[bB]54[bB][dD][aA]02913$",
              },
              amount: { const: tier.amount },
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
          x402_resource_url: { const: buyUrl },
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
      `Generate with protectedPaymentTierMarketplaceInput({tierId: "${tier.id}", `
      + "signer, payanOfferId, payment}). Caller proof signs every payment and "
      + "policy field plus this exact Payan buy URL."
    ),
  });
}

function outputSchema(tier) {
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
        required: ["contract", "payment", "policy", "decision", "protection"],
        properties: {
          contract: { const: "AGPD-1/1.0" },
          payment: {
            type: "object",
            properties: { amount: { const: tier.amount } },
            required: ["amount"],
          },
          policy: { type: "object" },
          decision: { enum: ["allow", "block"] },
          protection: {
            type: "object",
            properties: {
              contract: { const: "agent-guild/protected-value-policy/v1" },
              marketplace: {
                type: "object",
                properties: { tier_id: { const: tier.id } },
                required: ["tier_id", "x402_resource_url"],
              },
            },
            required: ["contract", "pricing", "marketplace"],
          },
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

async function registerTier(credential, tier) {
  const externalUrl = `${GUILD}/wallet-binding/protected-decision/tiers/${tier.id}`;
  const registration = await jsonRequest("/api/v1/offers", credential.api_key, {
    method: "POST",
    body: {
      title: `Protect Exact ${tier.notional} USDC Agent Payment — Agent Guild`,
      description: (
        `Before an autonomous wallet sends exactly ${tier.notional} USDC on Base, `
        + "obtain one short-lived signed AGPD-1 allow/block credential for the "
        + "exact payee and payment. Agent Guild requires active wallet identity, "
        + "current risk evidence, fresh verified routing and evidence depth for "
        + `high value at risk. The ${tier.fee} fee is the standard 25 bps—not `
        + "insurance, escrow or a delivery guarantee."
      ),
      category: "Security",
      tags: [
        "wallet-security", "payment-policy", "high-value-payment", "x402",
        "USDC", "Base", "AGPD-1", "signed-proof", "agent-guild",
      ],
      offerType: "api",
      externalUrl,
      httpMethod: "POST",
      verificationBody: {},
      inputSchema: JSON.stringify({
        type: "object", required: ["request", "caller_proof"],
        description: "Exact Payan buy URL is schema-pinned after registration.",
      }),
      outputSchema: outputSchema(tier),
      estimatedDurationSeconds: 3,
      previewDescription: (
        `Signed allow/block decision for an exact ${tier.notional} USDC Base payment; `
        + `service fee ${tier.fee}, with free verification and no API key.`
      ),
    },
  });
  const offerId = String(registration?.offerId || "");
  if (!offerId) throw new Error(`PayanAgent did not return an offerId for ${tier.id}`);
  const buyUrl = `${PAYAN}/x402/${offerId}`;
  await jsonRequest(`/api/v1/offers/${offerId}`, credential.api_key, {
    method: "PATCH",
    body: { inputSchema: inputSchema(tier, buyUrl), outputSchema: outputSchema(tier) },
  });
  return {
    tierId: tier.id, offerId, buyUrl,
    offerUrl: `${PAYAN}/marketplace/offers/${offerId}`,
    externalUrl,
  };
}

async function main() {
  if (process.argv.includes("--check")) {
    for (const tier of TIERS) {
      JSON.parse(inputSchema(tier, `${PAYAN}/x402/kh_protected_check_${tier.id}`));
      JSON.parse(outputSchema(tier));
    }
    process.stdout.write(JSON.stringify({
      ready: true, sellerId: SELLER_ID,
      tiers: TIERS.map(({ id, notional, fee }) => ({ id, notional, fee })),
      credentialSource: "macOS Keychain",
    }) + "\n");
    return;
  }
  const credential = recoverCredential();
  const offers = [];
  for (const tier of TIERS) offers.push(await registerTier(credential, tier));
  process.stdout.write(JSON.stringify({ registered: true, offers }) + "\n");
}

main().catch((error) => {
  process.stderr.write(String(error?.message || error) + "\n");
  process.exit(1);
});
