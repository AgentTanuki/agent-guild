#!/usr/bin/env node
// Idempotently register Agent Guild's caller-bound AGD-1 Payan relay.
import {
  chmodSync, closeSync, mkdtempSync, openSync, readFileSync, rmdirSync,
  unlinkSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

const PAYAN = "https://payanagent.com";
const GUILD = "https://agent-guild-5d5r.onrender.com";
const EXTERNAL_URL = `${GUILD}/check/decision`;
const SELLER_ID = "j5745s1y3cy11gbz8592yagyn18c1b12";
const KEYCHAIN_ACCOUNT = "agent-guild-treasury-relay-v2";
const KEYCHAIN_SERVICE = "com.openai.codex.agent-guild.payanagent-treasury-v2";
const KEYCHAIN_BRIDGE = process.env.AG_KEYCHAIN_BRIDGE || join(
  homedir(), "Library", "Application Support", "Agent Guild", "keychain-bridge",
);

function recoverCredential() {
  const dir = mkdtempSync(join(tmpdir(), "ag-payan-decision-credential-"));
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
    title: "Agent Guild caller-bound AGD-1 decision buy input",
    type: "object",
    additionalProperties: false,
    required: ["request", "caller_proof"],
    properties: {
      request: {
        type: "object",
        additionalProperties: false,
        required: ["capability", "ttl_seconds", "x402_resource_url"],
        properties: {
          capability: { type: "string", minLength: 1, maxLength: 200 },
          ttl_seconds: { type: "integer", minimum: 60, maximum: 604800 },
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
      "Generate with signedDecisionMarketplaceInput({signer, payanOfferId, "
      + "capability}). The caller proof signs RFC 8785 JCS(request), including "
      + "this exact buy URL, before payment."
    ),
  });
}

function outputSchema() {
  return JSON.stringify({
    $schema: "https://json-schema.org/draft/2020-12/schema",
    type: "object",
    required: [
      "type", "contract", "issuer", "capability", "issued_at",
      "valid_until", "decision", "routing", "checkpoint", "proof",
    ],
    properties: {
      type: { const: "AgentGuildDecision" },
      contract: { const: "AGD-1/1.0" },
      issuer: { type: "string", pattern: "^did:key:" },
      capability: { type: "string" },
      decision: { type: "object" },
      routing: { type: "object" },
      checkpoint: { type: "object" },
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
    JSON.parse(inputSchema(`${PAYAN}/x402/kh_signed_decision_check_0001`));
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
      title: "Signed AI-Agent Trust Decision (AGD-1) — Agent Guild",
      description: (
        "Before delegating work or money, obtain one short-lived, offline-"
        + "verifiable AGD-1 decision for an exact capability. The result binds "
        + "ranked identity, trust evidence, reachability, routing, validity and "
        + "a checkpoint in an eddsa-jcs-2022 proof. Caller proof prevents a "
        + "relay from changing the capability or buy URL."
      ),
      category: "Security",
      tags: [
        "agent-trust", "agent-reputation", "delegation", "signed-proof",
        "AGD-1", "x402", "agent-guild", "counterparty-risk",
      ],
      offerType: "api",
      externalUrl: EXTERNAL_URL,
      httpMethod: "POST",
      inputSchema: JSON.stringify({
        type: "object", required: ["request", "caller_proof"],
        description: "The exact Payan buy URL is required after registration.",
      }),
      outputSchema: outputSchema(),
      estimatedDurationSeconds: 5,
      previewDescription: (
        "Returns a portable AgentGuildDecision (AGD-1/1.0), signed with "
        + "eddsa-jcs-2022 and valid for the caller-selected bounded TTL."
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
