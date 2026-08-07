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
        url: GUILD_BASE,
        description:
          "Canonical Agent Guild API. All calls, payment challenges, settlements, signatures, and receipts terminate here.",
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
      "/evidence/verify": {
        post: {
          operationId: "verifyAgentGuildEvidenceBundle",
          summary: "Verify an Agent Guild evidence bundle",
          description:
            "Free verification of a previously issued evidence bundle. No account or payment is required.",
          tags: ["Trust before payment"],
          requestBody: {
            required: true,
            content: {
              "application/json": {
                schema: { type: "object", additionalProperties: true },
              },
            },
          },
          responses: {
            "200": {
              description: "Signature, expiry, and checkpoint verification result.",
              content: {
                "application/json": {
                  schema: { type: "object", additionalProperties: true },
                },
              },
            },
          },
          "x-price-usd": 0,
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
        "This worker neither receives nor forwards payments. Buyers call Agent Guild directly and verify Agent Guild's signatures and receipts.",
      accountingPolicy:
        "Only independently verified external mainnet or fiat USD revenue counts. Trial, sandbox, testnet, first-party, unverified, and self-funded activity is excluded.",
    },
  };
}
