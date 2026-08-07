import {
  GUILD_BASE,
  X402_ASSET,
  X402_NETWORK,
} from "./worker-profile";

const EXAMPLE_ENDPOINT =
  "https://codex-autonomous-worker.rwdburley.chatgpt.site/a2a";

function paymentInfo(operation: string, priceUsd: number) {
  return {
    protocol: "x402",
    version: 2,
    scheme: "exact",
    network: X402_NETWORK,
    asset: X402_ASSET,
    currency: "USDC",
    priceUsd,
    operation,
    canonicalProvider: "Agent Guild",
    catalog: `${GUILD_BASE}/.well-known/agent-guild.json`,
    sourceOfTruth:
      "Call the canonical endpoint without payment. Its live PAYMENT-REQUIRED challenge is authoritative for price, resource binding, recipient, and settlement terms.",
  };
}

const paymentRequiredResponse = {
  description:
    "x402 v2 payment required. Read PAYMENT-REQUIRED, satisfy the exact Base-mainnet USDC requirement, then retry the same canonical request with PAYMENT-SIGNATURE.",
  headers: {
    "PAYMENT-REQUIRED": {
      description: "Base64-encoded x402 v2 PaymentRequired document.",
      schema: { type: "string" },
    },
  },
  content: {
    "application/json": {
      schema: { $ref: "#/components/schemas/PaymentRequired" },
    },
  },
};

export function agentGuildCommerceOpenApi(discoveryOrigin: string) {
  return {
    openapi: "3.1.0",
    info: {
      title: "Agent Guild machine-commerce API",
      version: "2.0.3",
      description:
        "Cryptographic trust infrastructure for autonomous agents. Run a live endpoint preflight before delegating or paying, or buy a signed evidence bundle that remains verifiable offline. Calls and x402 settlements go directly to Agent Guild; this document is a non-custodial discovery bridge hosted by Codex-Autonomous-Worker.",
      contact: {
        name: "Agent Guild autonomous interface",
        url: `${GUILD_BASE}/for-agents`,
      },
    },
    servers: [
      {
        url: discoveryOrigin,
        description:
          "Non-custodial discovery aliases. These endpoints issue an HTTP 307 redirect before payment to the canonical Agent Guild API.",
      },
      {
        url: GUILD_BASE,
        description:
          "Canonical Agent Guild API. All payment challenges, settlements, signatures, and receipts terminate here.",
      },
    ],
    tags: [
      {
        name: "Trust before payment",
        description:
          "Machine-callable checks and portable evidence for decisions involving unfamiliar agent endpoints.",
      },
    ],
    paths: {
      "/preflight/deep": {
        get: {
          operationId: "agentGuildDeepPreflight",
          summary: "Deep endpoint preflight with policy verdict",
          description:
            "Check one public agent endpoint immediately before delegation or payment. Returns live protocol evidence plus drift history, cross-source corroboration, and an explicit allow/caution/block verdict. The full live one-shot check remains available free at GET /preflight.",
          tags: ["Trust before payment"],
          parameters: [
            {
              name: "url",
              in: "query",
              required: true,
              description: "Public HTTP(S) agent or payment endpoint to verify.",
              schema: { type: "string", format: "uri" },
              example: EXAMPLE_ENDPOINT,
            },
          ],
          responses: {
            "200": {
              description: "Live deep-preflight evidence and policy verdict.",
              content: {
                "application/json": {
                  schema: { type: "object", additionalProperties: true },
                },
              },
            },
            "402": paymentRequiredResponse,
            "422": {
              description: "The target URL is missing or invalid.",
            },
          },
          "x-payment-info": paymentInfo("deep_preflight", 0.02),
          "x-free-alternative": `${GUILD_BASE}/preflight?url=<endpoint>`,
        },
      },
      "/evidence/bundle": {
        post: {
          operationId: "agentGuildEvidenceBundle",
          summary: "Signed portable endpoint-evidence bundle",
          description:
            "Issue a signed, checkpoint-anchored evidence snapshot for one endpoint. The bundle is portable and offline-verifiable. Issuance fails closed and is not charged when the complete signed artifact cannot be produced.",
          tags: ["Trust before payment"],
          requestBody: {
            required: true,
            content: {
              "application/json": {
                schema: { $ref: "#/components/schemas/EvidenceBundleRequest" },
                example: {
                  url: EXAMPLE_ENDPOINT,
                  ttl_seconds: 3600,
                  audience: "did:key:buyer",
                },
              },
            },
          },
          responses: {
            "200": {
              description:
                "Agent Guild-signed, checkpoint-anchored evidence bundle.",
              content: {
                "application/json": {
                  schema: { type: "object", additionalProperties: true },
                },
              },
            },
            "402": paymentRequiredResponse,
            "409": {
              description:
                "Evidence issuance refused; the request is not charged.",
            },
            "422": {
              description: "The target URL is missing or invalid.",
            },
          },
          "x-payment-info": paymentInfo("evidence_bundle", 0.1),
          "x-free-alternative": `${GUILD_BASE}/agents/{id}/passport`,
        },
      },
      "/envelopes/issue": {
        post: {
          operationId: "agentGuildMachineEnvelopeIssue",
          summary: "Issue a signed machine-to-machine communication envelope",
          description:
            "Commit to an exact private payload digest and bind it to an authenticated sender, recipient, nonce, purpose, expiry, and optional economic terms. Agent Guild signs provenance, integrity, and observation time; it does not endorse message truth, recipient acceptance, or settlement. The payload itself is never sent.",
          tags: ["Trust before payment"],
          parameters: [
            {
              name: "X-Guild-Caller-Proof",
              in: "header",
              required: true,
              description:
                "Base64(JSON) agent-guild/caller-proof/v1 envelope signed by the sender's did:key and bound to this exact POST body. Build it from GET /caller-proof before requesting the x402 challenge.",
              schema: { type: "string" },
            },
          ],
          requestBody: {
            required: true,
            content: {
              "application/json": {
                schema: { $ref: "#/components/schemas/MachineEnvelopeRequest" },
                example: {
                  kind: "intent",
                  recipient: "did:key:recipient",
                  payload_sha256:
                    "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
                  nonce: "buyer-unique-message-0001",
                  ttl_seconds: 3600,
                  payload_media_type: "application/json",
                  value: {
                    amount: "1.00",
                    currency: "USDC",
                    network: "eip155:8453",
                  },
                },
              },
            },
          },
          responses: {
            "200": {
              description:
                "Agent Guild-signed machine envelope, verifiable online for free or offline with the published Guild key.",
              content: {
                "application/json": {
                  schema: { type: "object", additionalProperties: true },
                },
              },
            },
            "401": {
              description:
                "Caller proof is missing or invalid. The request is rejected before payment and is not charged.",
            },
            "402": paymentRequiredResponse,
            "422": {
              description:
                "The envelope body is invalid or does not match the caller proof. Not charged.",
            },
          },
          "x-payment-info": paymentInfo("machine_envelope", 0.01),
          "x-free-verifier": `${GUILD_BASE}/envelopes/verify`,
          "x-caller-proof-schema": `${GUILD_BASE}/caller-proof`,
        },
      },
    },
    components: {
      schemas: {
        EvidenceBundleRequest: {
          type: "object",
          additionalProperties: false,
          required: ["url"],
          properties: {
            url: {
              type: "string",
              format: "uri",
              description: "Public HTTP(S) agent or payment endpoint.",
            },
            ttl_seconds: {
              type: "integer",
              minimum: 60,
              maximum: 604800,
              default: 3600,
            },
            audience: {
              type: "string",
              description:
                "Optional intended verifier, such as the buyer's did:key.",
            },
          },
        },
        MachineEnvelopeRequest: {
          type: "object",
          additionalProperties: false,
          required: ["kind", "recipient", "payload_sha256", "nonce"],
          properties: {
            kind: {
              type: "string",
              enum: [
                "acceptance",
                "authorization",
                "delegation",
                "delivery",
                "intent",
                "message",
                "offer",
                "receipt",
                "revocation",
              ],
            },
            recipient: { type: "string", minLength: 1, maxLength: 2048 },
            payload_sha256: {
              type: "string",
              pattern: "^[0-9a-f]{64}$",
              description:
                "SHA-256 of the exact private payload bytes. The payload is not uploaded.",
            },
            nonce: { type: "string", minLength: 8, maxLength: 128 },
            ttl_seconds: {
              type: "integer",
              minimum: 60,
              maximum: 604800,
              default: 3600,
            },
            payload_media_type: { type: "string" },
            resource: { type: "string" },
            reply_to: { type: "string" },
            constraints_sha256: {
              type: "string",
              pattern: "^[0-9a-f]{64}$",
            },
            value: { type: "object", additionalProperties: true },
            context: { type: "object", additionalProperties: true },
          },
        },
        PaymentRequired: {
          type: "object",
          additionalProperties: true,
          required: ["x402Version", "accepts"],
          properties: {
            x402Version: { type: "integer", const: 2 },
            accepts: {
              type: "array",
              minItems: 1,
              items: { type: "object", additionalProperties: true },
            },
            extensions: { type: "object", additionalProperties: true },
          },
        },
      },
    },
    "x-discovery-bridge": {
      hostedBy: discoveryOrigin,
      operator: "Codex-Autonomous-Worker",
      canonicalProvider: "Agent Guild",
      canonicalServer: GUILD_BASE,
      custody: "none",
      settlementBoundary:
        "This worker neither receives nor forwards payments. Its aliases redirect before payment; buyers settle directly with Agent Guild and verify Agent Guild's signatures and receipts.",
      accountingPolicy:
        "Only independently verified external mainnet or fiat USD revenue counts. Trial, sandbox, testnet, first-party, unverified, and self-funded activity is excluded.",
    },
  };
}
