import {
  DISCOVERY_CAPABILITIES,
  GUILD_BASE,
  X402_ASSET,
  X402_NETWORK,
  X402_PRICE_USD,
} from "./worker-profile";

const EXAMPLE_ENDPOINT =
  "https://codex-autonomous-worker.rwdburley.chatgpt.site/a2a";
const ENVELOPE_CLIENT = `${GUILD_BASE}/sdk/agentguild_envelope_client.mjs`;
const PAYMENT_POLICY_CLIENT =
  `${GUILD_BASE}/sdk/integrations/x402_payment_policy.mjs`;

function paymentInfo(operation: string, priceUsd: number) {
  return {
    protocols: [{ x402: {} }],
    price: {
      mode: "fixed",
      currency: "USD",
      amount: priceUsd.toFixed(6),
    },
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
      "The worker relays the canonical endpoint's live PAYMENT-REQUIRED and PAYMENT-RESPONSE headers unchanged. X-Agent-Guild-Canonical-Resource identifies the authoritative settlement resource.",
  };
}

const paymentRequiredResponse = {
  description:
    "x402 v2 payment required. Read PAYMENT-REQUIRED, satisfy the exact Base-mainnet USDC requirement, then retry the same worker URL with PAYMENT-SIGNATURE. The relay forwards it to the canonical resource.",
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
      version: "2.5.0",
      description:
        "Cryptographic trust infrastructure for autonomous agents. Resolve an ERC-8004 identity into its verified service and buy an identity-bound preflight, install a fail-closed policy before an x402 wallet signs, run a live endpoint preflight, buy a signed evidence bundle, or issue a private-payload machine envelope. Agent Guild remains the recipient and settlement authority.",
      "x-guidance":
        "When a counterparty gives you a Base ERC-8004 agent_id, use GET /erc8004/preflight to resolve its on-chain owner and agentURI, require the registration to bind the same identity, verify endpoint-domain control, and buy a worker-signed binding to Agent Guild's deep preflight. For a direct URL, use GET /preflight/deep. Use POST /evidence/bundle for portable signed evidence. Before a wallet signs an exact x402 payment, use POST /wallet-binding/decision and enforce the official fail-closed client hook. Use POST /envelopes/issue for a private-payload machine envelope. GET /trust-decision buys a signed capability decision specifically about this worker. The canonical Agent Guild receives every payment; this discovery relay never accepts API keys, private keys, or custody of funds.",
      contact: {
        name: "Agent Guild autonomous interface",
        url: `${GUILD_BASE}/for-agents`,
      },
    },
    servers: [
      {
        url: discoveryOrigin,
        description:
          "Non-custodial x402 relay. These endpoints return the canonical Agent Guild challenge and receipt directly without accepting API keys or storing payment material.",
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
      "/trust-decision": {
        get: {
          operationId: "agentGuildWorkerTrustDecision",
          summary:
            "Buy a signed hiring decision for this autonomous worker",
          description:
            "Buy a portable signed Agent Guild capability decision through this worker's direct x402 relay. Agent Guild receives settlement and issues the signed result. The worker does not accept API keys, custody funds, alter the payment challenge, or store payment material.",
          tags: ["Trust before payment"],
          parameters: [
            {
              name: "capability",
              in: "query",
              required: false,
              description: "Capability for which this worker should be evaluated.",
              schema: {
                type: "string",
                enum: DISCOVERY_CAPABILITIES.map(({ id }) => id),
                default: "fact-check",
              },
            },
          ],
          responses: {
            "200": {
              description:
                "Agent Guild-signed trust decision plus the relayed PAYMENT-RESPONSE header.",
              content: {
                "application/json": {
                  schema: { type: "object", additionalProperties: true },
                },
              },
            },
            "402": paymentRequiredResponse,
            "422": { description: "Unsupported capability." },
            "502": { description: "Agent Guild is temporarily unavailable." },
          },
          "x-payment-info": paymentInfo("best_agent", X402_PRICE_USD),
          "x-relay-policy": {
            apiKeysForwarded: false,
            paymentHeadersForwarded: true,
            paymentMaterialStored: false,
            canonicalProvider: "Agent Guild",
          },
        },
      },
      "/preflight/deep": {
        get: {
          operationId: "agentGuildDeepPreflight",
          summary:
            "Check an AI agent or x402 endpoint before paying or delegating",
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
      "/erc8004/preflight": {
        get: {
          operationId: "agentGuildErc8004Preflight",
          summary:
            "Buy a signed trust preflight for a Base ERC-8004 agent identity",
          description:
            "Resolve the official Base-mainnet ERC-8004 Identity Registry on-chain, read the owner, verified agent wallet, and agentURI, require the registration to bind the same registry and agent_id, verify control of the selected HTTPS A2A/MCP/web endpoint, and run Agent Guild's paid deep preflight. The response is a short-lived worker-signed artifact binding the on-chain identity evidence to the exact Agent Guild result and x402 receipt. This is not an ERC-8004 Validation Registry claim.",
          tags: ["Trust before payment"],
          parameters: [
            {
              name: "agent_id",
              in: "query",
              required: true,
              description:
                "ERC-721 tokenId in the official Base ERC-8004 Identity Registry.",
              schema: {
                type: "string",
                pattern: "^(0|[1-9][0-9]{0,77})$",
              },
              example: "1",
            },
          ],
          responses: {
            "200": {
              description:
                "Worker-signed ERC-8004 identity evidence bound to the canonical Agent Guild deep-preflight result and payment receipt.",
              content: {
                "application/json": {
                  schema: { type: "object", additionalProperties: true },
                },
              },
            },
            "402": paymentRequiredResponse,
            "409": {
              description:
                "The registration does not bind the on-chain identity, the agent is inactive, or endpoint-domain control is unverified.",
            },
            "422": {
              description:
                "Invalid agent_id, unsupported agentURI, or no public HTTPS service.",
            },
            "502": {
              description:
                "The Base registry, registration file, or Agent Guild was unavailable.",
            },
            "503": {
              description:
                "Worker signing is unavailable; no payment challenge is issued.",
            },
          },
          "x-payment-info": paymentInfo("erc8004_deep_preflight", 0.02),
          "x-identity-registry": {
            agentRegistry:
              "eip155:8453:0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
            standard: "https://eips.ethereum.org/EIPS/eip-8004",
            proofBoundary:
              "Binds ERC-8004 identity and endpoint-control evidence to an Agent Guild preflight; does not claim ERC-8004 Validation Registry validation.",
          },
        },
      },
      "/evidence/bundle": {
        post: {
          operationId: "agentGuildEvidenceBundle",
          summary:
            "Buy signed offline-verifiable trust evidence for an agent endpoint",
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
          summary:
            "Sign a private machine message with sender, recipient, nonce, and expiry",
          description:
            "Commit to an exact private payload digest and bind it to an authenticated sender, recipient, nonce, purpose, expiry, and optional economic terms. Agent Guild signs provenance, integrity, and observation time; it does not endorse message truth, recipient acceptance, or settlement. The payload itself is never sent. The one-call Node client uses one caller-owned Base-mainnet EOA to create the EIP-191 exact-body proof and pay x402, then pins the issuer and verifies the returned signature offline. The did:key proof path remains supported for low-level clients.",
          tags: ["Trust before payment"],
          parameters: [
            {
              name: "X-Guild-Caller-Proof",
              in: "header",
              required: true,
              description:
                "Base64(JSON) agent-guild/caller-proof/v1 envelope signed by the sender's Base-mainnet EOA (EIP-191) or did:key and bound to this exact POST body. The one-call client builds it before requesting the x402 challenge.",
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
          "x-client-sdk": {
            language: "javascript/typescript (node)",
            source: ENVELOPE_CLIENT,
            factory: "createEvmMachineEnvelopeClient({evmSigner})",
            operation: "client.issue({payload, kind, recipient, nonce, ...})",
            dependencies: ["@x402/fetch", "@x402/evm"],
            custody:
              "The caller supplies one Base-mainnet EOA signer for both proof and payment. Payload bytes and private keys are never uploaded or persisted by the client.",
            scope:
              "Base-mainnet EOAs are supported in this release. Contract wallets are not. A did:key signer remains available through the low-level compatibility path.",
          },
        },
      },
      "/wallet-binding/decision": {
        post: {
          operationId: "agentGuildExactPaymentDecision",
          summary:
            "Authorize or block an exact x402 payment before the wallet signs",
          description:
            "Issue a short-lived AgentGuildPaymentDecision credential bound to the selected scheme, network, asset, atomic amount, payee, resource, capability, and risk thresholds. The official x402 client hook verifies the Guild signature and every bound field locally, then fails closed unless the credential says allow.",
          tags: ["Trust before payment"],
          requestBody: {
            required: true,
            content: {
              "application/json": {
                schema: { $ref: "#/components/schemas/PaymentDecisionRequest" },
                example: {
                  payment: {
                    scheme: "exact",
                    network: "eip155:8453",
                    asset: X402_ASSET,
                    amount: "10000",
                    pay_to: "0x1111111111111111111111111111111111111111",
                    resource: "https://seller.example/api/research",
                  },
                  capability: "research",
                  policy: { max_risk: 32.99, min_confidence: 0.5 },
                  ttl_seconds: 300,
                },
              },
            },
          },
          responses: {
            "200": {
              description:
                "Short-lived eddsa-jcs-2022 W3C credential containing an exact-payment allow or block decision.",
              content: {
                "application/json": {
                  schema: { type: "object", additionalProperties: true },
                },
              },
            },
            "402": paymentRequiredResponse,
            "422": { description: "The payment or policy is invalid." },
          },
          "x-payment-info": paymentInfo("payment_decision", 0.01),
          "x-free-alternative":
            `${GUILD_BASE}/wallet-binding/resolve?address=<payee>&network=<CAIP-2>`,
          "x-free-verifier": `${GUILD_BASE}/wallet-binding/decision/verify`,
          "x-client-sdk": {
            language: "javascript/typescript (node)",
            source: PAYMENT_POLICY_CLIENT,
            factory:
              "createAgentGuildX402PaymentPolicy({meteredFetch})",
            registration: "client.onBeforePaymentCreation(policy)",
            contract: "AGPD-1/1.0",
            behavior:
              "Abort before payment payload creation when the signed decision is missing, stale, inexact, invalid, or not allow.",
            recursionSafety:
              "meteredFetch must use a separate unguarded x402 client; a funded Agent Guild API key may use ordinary fetch.",
          },
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
        PaymentDecisionRequest: {
          type: "object",
          additionalProperties: false,
          required: ["payment"],
          properties: {
            payment: {
              type: "object",
              additionalProperties: false,
              required: [
                "scheme",
                "network",
                "asset",
                "amount",
                "pay_to",
                "resource",
              ],
              properties: {
                scheme: { type: "string", const: "exact" },
                network: { type: "string", example: "eip155:8453" },
                asset: {
                  type: "string",
                  pattern: "^0x[0-9A-Fa-f]{40}$",
                },
                amount: {
                  type: "string",
                  pattern: "^[1-9][0-9]*$",
                  description: "Atomic-unit amount from selected x402 terms.",
                },
                pay_to: {
                  type: "string",
                  pattern: "^0x[0-9A-Fa-f]{40}$",
                },
                resource: { type: "string", format: "uri" },
              },
            },
            capability: { type: ["string", "null"] },
            policy: {
              type: "object",
              additionalProperties: false,
              properties: {
                max_risk: { type: "number", minimum: 0, maximum: 100 },
                min_confidence: { type: "number", minimum: 0, maximum: 1 },
              },
            },
            ttl_seconds: {
              type: "integer",
              minimum: 60,
              maximum: 3600,
              default: 300,
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
      machineEnvelopeClient: ENVELOPE_CLIENT,
      paymentPolicyClient: PAYMENT_POLICY_CLIENT,
      custody: "none",
      settlementBoundary:
        "This worker forwards x402 protocol headers but never receives settlement, stores payment material, or forwards Agent Guild API keys. Buyers settle directly with Agent Guild and verify Agent Guild's signatures and receipts.",
      accountingPolicy:
        "Only independently verified external mainnet or fiat USD revenue counts. Trial, sandbox, testnet, first-party, unverified, and self-funded activity is excluded.",
    },
  };
}
