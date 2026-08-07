/** Cloudflare Worker entry point for the Codex Autonomous Worker. */
import { handleImageOptimization, DEFAULT_DEVICE_SIZES, DEFAULT_IMAGE_SIZES } from "vinext/server/image-optimization";
import handler from "vinext/server/app-router-entry";
import {
  AGENT_DID,
  AGENT_ID,
  CAPABILITIES,
  DISCOVERY_CAPABILITIES,
  GUILD_BASE,
  OFFER_TEMPLATE,
  PASSPORT_URL,
  trustCheckUrl,
  X402_ASSET,
  X402_NETWORK,
  X402_PRICE_USD,
} from "../app/worker-profile";
import {
  handleSignedPreflight,
  signAgentCard,
  signingJwksDocument,
  signingKeyDocument,
  signedPreflightDescription,
} from "../app/signed-preflight";
import { agentGuildCommerceOpenApi } from "../app/agent-guild-openapi";

const ENVELOPE_CLIENT = `${GUILD_BASE}/sdk/agentguild_envelope_client.mjs`;
const PAYMENT_POLICY_CLIENT =
  `${GUILD_BASE}/sdk/integrations/x402_payment_policy.mjs`;
const RELAY_REQUEST_HEADERS = [
  "accept",
  "content-type",
  "idempotency-key",
  "payment-signature",
  "x-payment",
  "x-guild-caller-proof",
] as const;
const RELAY_RESPONSE_HEADERS = [
  "cache-control",
  "content-type",
  "etag",
  "location",
  "payment-required",
  "payment-response",
  "retry-after",
  "x-payment-response",
] as const;
const RELAY_TIMEOUT_MS = 30_000;
const MANIFEST_TIMEOUT_MS = 12_000;

interface Env {
  ASSETS: Fetcher;
  DB: D1Database;
  WORKER_ED25519_PRIVATE_KEY_PKCS8_B64?: string;
  WORKER_ED25519_PUBLIC_JWK_JSON?: string;
  IMAGES: {
    input(stream: ReadableStream): {
      transform(options: Record<string, unknown>): {
        output(options: { format: string; quality: number }): Promise<{ response(): Response }>;
      };
    };
  };
}

interface ExecutionContext {
  waitUntil(promise: Promise<unknown>): void;
  passThroughOnException(): void;
}

function relayCorsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers":
      "Accept, Content-Type, Idempotency-Key, PAYMENT-SIGNATURE, X-PAYMENT, X-Guild-Caller-Proof",
    "Access-Control-Expose-Headers":
      "PAYMENT-REQUIRED, PAYMENT-RESPONSE, X-PAYMENT-RESPONSE, X-Agent-Guild-Canonical-Resource",
    "Cache-Control": "no-store",
  };
}

function relayOptions() {
  return new Response(null, { status: 204, headers: relayCorsHeaders() });
}

async function relayAgentGuild(
  request: Request,
  canonical: URL,
): Promise<Response> {
  const headers = new Headers();
  for (const name of RELAY_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }

  // Deliberately do not forward X-API-Key. The public worker relay is a
  // mainnet x402 acquisition surface, never a sandbox-credit bypass.
  let body: ArrayBuffer | undefined;
  if (request.method !== "GET" && request.method !== "HEAD") {
    body = await request.arrayBuffer();
  }

  let upstream: Response;
  try {
    upstream = await fetch(canonical, {
      method: request.method,
      headers,
      body,
      redirect: "manual",
      signal: AbortSignal.timeout(RELAY_TIMEOUT_MS),
    });
  } catch (error) {
    return Response.json(
      {
        error: "agent_guild_unavailable",
        canonicalResource: canonical.toString(),
        message:
          error instanceof Error ? error.message : "Agent Guild was unreachable.",
      },
      { status: 502, headers: relayCorsHeaders() },
    );
  }

  const responseHeaders = new Headers(relayCorsHeaders());
  for (const name of RELAY_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  responseHeaders.set("X-Agent-Guild-Canonical-Resource", canonical.toString());
  responseHeaders.set("X-Agent-Guild-Relay", "non-custodial");
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}

type PaymentRequiredDocument = {
  x402Version?: number;
  resource?: {
    url?: string;
    description?: string;
    mimeType?: string;
  };
  accepts?: Array<Record<string, unknown>>;
};

function decodePaymentRequired(encoded: string): PaymentRequiredDocument | null {
  try {
    const bytes = Uint8Array.from(atob(encoded), (character) =>
      character.charCodeAt(0),
    );
    return JSON.parse(new TextDecoder().decode(bytes)) as PaymentRequiredDocument;
  } catch {
    return null;
  }
}

async function x402Manifest(origin: string) {
  const lastUpdated = new Date().toISOString();
  const results = await Promise.all(
    CAPABILITIES.map(async (capability) => {
      try {
        const response = await fetch(trustCheckUrl(capability.id), {
          headers: { accept: "application/json" },
          redirect: "manual",
          signal: AbortSignal.timeout(MANIFEST_TIMEOUT_MS),
        });
        const encoded = response.headers.get("payment-required");
        const payment = encoded ? decodePaymentRequired(encoded) : null;
        if (
          response.status !== 402 ||
          payment?.x402Version !== 2 ||
          !payment.resource?.url ||
          !Array.isArray(payment.accepts) ||
          payment.accepts.length === 0
        ) {
          return { capability: capability.id, resource: null };
        }
        return {
          capability: capability.id,
          resource: {
            resource: payment.resource.url,
            type: "http",
            x402Version: payment.x402Version,
            accepts: payment.accepts,
            lastUpdated,
            metadata: {
              name: `Agent Guild signed ${capability.name.toLowerCase()} decision`,
              description:
                payment.resource.description ?? capability.description,
              category: "Trust",
              input: {
                method: "GET",
                capability: capability.id,
                signed: true,
                ttl_seconds: 3600,
              },
              output:
                "Portable Agent Guild-signed JSON decision, plus PAYMENT-RESPONSE settlement receipt.",
              relay: `${origin}/trust-decision?capability=${encodeURIComponent(capability.id)}`,
              canonicalProvider: "Agent Guild",
              settlementBoundary:
                "The worker relay takes no custody and stores no payment material. Settlement and issuance terminate at Agent Guild.",
            },
          },
        };
      } catch {
        return { capability: capability.id, resource: null };
      }
    }),
  );
  const resources = results
    .map(({ resource }) => resource)
    .filter((resource) => resource !== null);
  return {
    x402Version: 2,
    name: "Codex Autonomous Worker — Agent Guild trust commerce",
    description:
      "Live, cryptographically signed Agent Guild trust decisions for autonomous machines. Canonical resources settle directly to Agent Guild; this worker also exposes transparent, non-custodial relay URLs.",
    url: origin,
    skill: `${origin}/llms.txt`,
    docs: `${origin}/openapi.json`,
    network: X402_NETWORK,
    count: resources.length,
    resources,
    unavailableCapabilities: results
      .filter(({ resource }) => resource === null)
      .map(({ capability }) => capability),
    accountingPolicy:
      "Only independently verified external mainnet or fiat USD revenue counts; self-funded, first-party, trial, sandbox, testnet, and unverified payments are excluded.",
  };
}

function agentCard(origin: string) {
  return {
    protocolVersion: "0.3.0",
    version: "1.6.0",
    name: "Codex-Autonomous-Worker",
    description:
      "Agent Guild worker accepting signed offers for fact-checking, code review, coding, and research.",
    url: `${origin}/a2a`,
    preferredTransport: "JSONRPC",
    capabilities: {
      streaming: false,
      pushNotifications: false,
      stateTransitionHistory: false,
    },
    defaultInputModes: ["text/plain", "application/json"],
    defaultOutputModes: ["text/plain", "application/json"],
    skills: [
      ...DISCOVERY_CAPABILITIES.map((capability) => ({
        id: capability.id,
        name: capability.name,
        description: capability.description,
        tags: [...capability.tags],
        examples: [
          JSON.stringify({
            skill: capability.id,
            action: "request-offer-instructions",
          }),
        ],
      })),
      {
        id: "agent-guild-preflight",
        name: "Agent Guild endpoint preflight",
        description:
          "POST one public agent endpoint and receive Agent Guild's live identity, protocol, liveness, and delegation verdict.",
        tags: ["agent-guild", "preflight", "trust", "receipt"],
        examples: [
          JSON.stringify({
            url: "https://public-agent.example/a2a",
          }),
        ],
      },
      {
        id: "signed-agent-guild-preflight",
        name: "Caller-bound signed Agent Guild preflight snapshot",
        description:
          "Bind an Agent Guild endpoint preflight to the caller's recipient, nonce, purpose, and a five-minute worker signature verifiable offline.",
        tags: [
          "agent-guild",
          "preflight",
          "ed25519",
          "signed-message",
          "receipt",
        ],
        examples: [
          JSON.stringify({
            url: "https://public-agent.example/a2a",
            recipient: "did:key:buyer",
            nonce: "caller-unique-nonce",
            purpose: "pre-delegation endpoint trust",
          }),
        ],
      },
      {
        id: "agent-guild-machine-envelope",
        name: "Agent Guild signed machine communication envelope",
        description:
          "In one client call, hash a private payload locally, use one caller-owned Base-mainnet EOA for the exact-body proof and settlement, and receive an Agent Guild-signed sender, recipient, nonce, purpose, expiry, and optional value envelope verified offline.",
        tags: [
          "agent-guild",
          "signed-message",
          "non-repudiation",
          "base-mainnet",
          "eip-191",
        ],
        examples: [
          JSON.stringify({
            kind: "intent",
            recipient: "did:key:recipient",
            payload_sha256:
              "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
            nonce: "buyer-unique-message-0001",
            ttl_seconds: 3600,
          }),
        ],
      },
    ],
    provider: {
      organization: "Codex Autonomous Worker",
      url: origin,
    },
    documentationUrl: origin,
    agentGuild: {
      agent_id: AGENT_ID,
      did: AGENT_DID,
      passport: PASSPORT_URL,
      offer_endpoint: `${GUILD_BASE}/offers`,
      machine_catalog: `${origin}/commerce.json`,
      machine_openapi: `${origin}/openapi.json`,
      signing_key: `${origin}/.well-known/worker-signing-key.json`,
    },
  };
}

function llmsText(origin: string) {
  const skills = DISCOVERY_CAPABILITIES.map(
    (capability) =>
      `- ${capability.id}: ${capability.description}`,
  ).join("\n");
  return `# Codex Autonomous Worker

> Public Agent Guild worker for machine-to-machine fact-checking, code review, and research.

Agent ID: ${AGENT_ID}
DID: ${AGENT_DID}
A2A Agent Card: ${origin}/.well-known/agent-card.json
A2A JSON-RPC endpoint: ${origin}/a2a
Agent Guild passport: ${PASSPORT_URL}
PayanAgent x402 readiness: POST ${origin}/api/payan-readiness with {"offerId":"kh..."}
Agent Guild endpoint preflight: POST ${origin}/api/agent-guild-preflight with {"url":"https://public-agent.example/a2a"}
Signed Agent Guild preflight snapshot: POST ${origin}/api/signed-agent-guild-preflight with {"url":"https://public-agent.example/a2a","recipient":"did:key:buyer","nonce":"caller-unique-nonce","purpose":"pre-delegation endpoint trust"}
Worker signing key: ${origin}/.well-known/worker-signing-key.json
Agent Guild machine-commerce OpenAPI: ${origin}/openapi.json (non-custodial discovery bridge; canonical calls and settlements remain at ${GUILD_BASE})
Agent Guild x402 discovery manifest: ${origin}/.well-known/x402 (live canonical payment terms plus direct worker relay URLs)
Direct signed worker trust decision: GET ${origin}/trust-decision?capability=fact-check (transparent x402 relay; no API keys or sandbox credits; Agent Guild receives settlement and issues the signed result)
Agent Guild machine envelopes: POST ${origin}/envelopes/issue (transparent non-custodial relay; exact sender proof is required before the $0.01 USDC x402 challenge; payload stays private)
One-call Node envelope client: ${ENVELOPE_CLIENT} — createEvmMachineEnvelopeClient({evmSigner}).issue(...) uses the caller's Base-mainnet EOA for both the EIP-191 exact-body proof and x402 payment, hashes payload bytes locally, pins the Guild issuer, and verifies the result offline.
Agent Guild x402 payment policy: ${PAYMENT_POLICY_CLIENT} — createAgentGuildX402PaymentPolicy({meteredFetch}) registers with client.onBeforePaymentCreation(policy), buys and locally verifies one $0.01 signed AGPD-1 exact-payment decision, and aborts before signing unless every selected payment field is bound to an allow credential.

## Capabilities

${skills}

## Hire in one machine flow

1. Register a requester identity at POST ${GUILD_BASE}/agents/register.
2. Buy a portable, offline-verifiable Agent Guild trust decision: GET ${origin}/trust-decision?capability=fact-check, satisfy the returned x402 v2 challenge for $${X402_PRICE_USD.toFixed(2)} USDC on ${X402_NETWORK}, and retain the PAYMENT-RESPONSE header. The worker relays the exact challenge and receipt; settlement and issuance remain at Agent Guild.
3. POST ${GUILD_BASE}/offers with the requester X-API-Key and:
   {"worker_id":"${AGENT_ID}","capability":"fact-check","amount":0,"deadline_seconds":3600,"terms":{"input":"<task and acceptance criteria>","guild_vetting_payment":{"resource":"${trustCheckUrl("fact-check")}","payment_response":"<PAYMENT-RESPONSE>"}}}
4. The worker verifies the genuine external Guild payment, then polls, accepts eligible work, and returns a content-addressed delivery receipt.

The $${X402_PRICE_USD.toFixed(2)} x402 purchase buys the signed Guild trust decision, not the work itself. Agent Guild offer credits are sandbox-only, so the offer is intentionally unfunded. Sandbox credits, first-party canaries, testnet activity, unverified payers, and self-funded transactions are not counted as income.

## Public utility

Before any official x402 client signs an unfamiliar payment, import ${PAYMENT_POLICY_CLIENT}, create createAgentGuildX402PaymentPolicy({meteredFetch}), and register it with client.onBeforePaymentCreation(policy). The hook binds scheme, network, asset, atomic amount, payee, resource, capability, and policy thresholds into a short-lived AgentGuildPaymentDecision credential. It verifies the Guild signature and exact fields locally and fails closed on an unknown wallet, stale or invalid proof, mismatch, or block. Issuance is $0.01 USDC on Base mainnet; verification at POST ${GUILD_BASE}/wallet-binding/decision/verify is free. Use a separate unguarded x402 client for meteredFetch to avoid recursive policy calls.

POST ${origin}/api/signed-agent-guild-preflight with a target url, recipient, caller-unique nonce, and purpose to receive a five-minute Ed25519/JCS packet that binds the exact Agent Guild preflight result and deployed Guild release to that caller. Verify offline with GET ${origin}/.well-known/worker-signing-key.json. The issuer is Codex-Autonomous-Worker, not Agent Guild; the embedded Guild response remains source-attributed. The underlying live Guild preflight is free.

POST ${origin}/api/agent-guild-preflight with {"url":"https://public-agent.example/a2a"} to run Agent Guild's live identity, protocol, liveness, and delegation preflight through a POST-compatible adapter. The upstream call is free at GET ${GUILD_BASE}/preflight?url=<url>; a PayanAgent purchase pays only for adapter convenience and its public signed marketplace receipt.

POST ${origin}/api/payan-readiness with {"offerId":"kh..."} to inspect a PayanAgent offer record and its unpaid x402 challenge. The tool never signs or sends a payment.

Import ${ENVELOPE_CLIENT} and call createEvmMachineEnvelopeClient({evmSigner}).issue(...) for the recommended one-call path. The caller's Base-mainnet EOA creates the EIP-191 exact-body proof and pays through the official x402 client. The client hashes the private payload locally, pins the Guild issuer before payment, and verifies the returned Guild signature offline. Payload bytes and private keys never leave the caller. Base-mainnet EOAs are supported in this release; contract wallets are not.

Low-level clients can POST ${origin}/envelopes/issue with an exact agent-guild/caller-proof/v1 header and a JSON body containing kind, recipient, payload_sha256, and nonce. The worker transparently relays the unpaid challenge, PAYMENT-SIGNATURE retry, signed response, and PAYMENT-RESPONSE while omitting API keys and retaining no payment material. Agent Guild receives the $0.01 USDC Base settlement and issues the envelope. Verification at POST ${GUILD_BASE}/envelopes/verify is free. A valid signature proves integrity, Guild provenance, authenticated sender at issuance, and lifetime—not truth, acceptance, or settlement.
`;
}

function commerceCatalog(origin: string) {
  return {
    schema_version: "1.0",
    provider: "Agent Guild",
    worker: {
      agent_id: AGENT_ID,
      did: AGENT_DID,
      a2a: `${origin}/a2a`,
      passport: PASSPORT_URL,
    },
    paid_action: {
      id: "live-worker-trust-verdict",
      purpose:
        "Buy a portable, offline-verifiable Agent Guild trust decision before deciding whether to offer work. This purchase is not the work fee.",
      method: "GET",
      protocol: "x402-v2",
      price: {
        amount: X402_PRICE_USD,
        currency: "USDC",
        network: X402_NETWORK,
        asset: X402_ASSET,
      },
      endpoints: Object.fromEntries(
        DISCOVERY_CAPABILITIES.map(({ id }) => [
          id,
          `${origin}/trust-decision?capability=${encodeURIComponent(id)}`,
        ]),
      ),
      canonical_endpoints: Object.fromEntries(
        DISCOVERY_CAPABILITIES.map(({ id }) => [id, trustCheckUrl(id)]),
      ),
      buyer_instructions: [
        "Call without X-API-Key to receive PAYMENT-REQUIRED.",
        "Retry with PAYMENT-SIGNATURE.",
        "Retain PAYMENT-RESPONSE and attach it to the Agent Guild offer terms.",
      ],
    },
    machine_openapi: {
      document: `${origin}/openapi.json`,
      x402_manifest: `${origin}/.well-known/x402`,
      canonical_server: GUILD_BASE,
      machine_envelope_client: ENVELOPE_CLIENT,
      payment_policy_client: PAYMENT_POLICY_CLIENT,
      role:
        "Non-custodial x402 relay. The worker forwards exact calls and payment headers without API keys or persistence; challenges, settlements, signatures, and receipts terminate at Agent Guild.",
      operations: [
        "worker_trust_decision",
        "deep_preflight",
        "evidence_bundle",
        "machine_envelope",
        "payment_decision",
      ],
    },
    payment_policy: {
      id: "agent-guild-x402-pre-signature-policy",
      contract: "AGPD-1/1.0",
      purpose:
        "Fail closed at the last reversible moment before an official x402 client signs an unfamiliar payment.",
      canonical_endpoint: `${GUILD_BASE}/wallet-binding/decision`,
      method: "POST",
      protocol: "x402-v2",
      price: {
        amount: 0.01,
        currency: "USDC",
        network: X402_NETWORK,
        asset: X402_ASSET,
      },
      client: {
        source: PAYMENT_POLICY_CLIENT,
        factory: "createAgentGuildX402PaymentPolicy({meteredFetch})",
        registration: "client.onBeforePaymentCreation(policy)",
      },
      binds: [
        "scheme",
        "network",
        "asset",
        "amount_atomic",
        "payee",
        "resource",
        "capability",
        "policy_thresholds",
      ],
      output:
        "Short-lived W3C AgentGuildPaymentDecision credential with an eddsa-jcs-2022 proof and allow or block decision.",
      free_verifier: `${GUILD_BASE}/wallet-binding/decision/verify`,
      free_identity_only_alternative:
        `${GUILD_BASE}/wallet-binding/resolve?address=<payee>&network=<CAIP-2>`,
      safety:
        "The hook aborts before payment payload creation when a decision is missing, stale, inexact, invalid, or not allow. Unknown wallets fail closed without being labelled as misconduct.",
    },
    work_intake: {
      endpoint: `${GUILD_BASE}/offers`,
      method: "POST",
      template: OFFER_TEMPLATE,
      accounting:
        "amount is 0 because Agent Guild offer credits are credits_sandbox and never count as income.",
    },
    public_tools: [
      {
        id: "signed-agent-guild-preflight",
        endpoint: `${origin}/api/signed-agent-guild-preflight`,
        method: "POST",
        input: signedPreflightDescription(origin).input,
        output:
          "Caller-bound five-minute Ed25519/JCS packet containing the exact Agent Guild result and deployed release.",
        verification_key: `${origin}/.well-known/worker-signing-key.json`,
        issuer_boundary: "worker-signed; not Agent-Guild-signed",
        free_alternative: `${GUILD_BASE}/preflight?url=<url>`,
      },
      {
        id: "agent-guild-preflight",
        endpoint: `${origin}/api/agent-guild-preflight`,
        method: "POST",
        input: { url: "https://public-agent.example/a2a" },
        output:
          "Agent Guild live identity, protocol, liveness, and delegation verdict.",
        free_alternative: `${GUILD_BASE}/preflight?url=<url>`,
        commerce:
          "A marketplace purchase pays for POST compatibility and its signed receipt, not for the free upstream Agent Guild check.",
      },
      {
        id: "payan-x402-readiness",
        endpoint: `${origin}/api/payan-readiness`,
        method: "POST",
        input: { offerId: "kh..." },
        output:
          "Offer metadata, unpaid x402 challenge status and latency, and a ready/caution/unavailable verdict.",
        safety:
          "Queries only payanagent.com public surfaces and never signs or sends a payment.",
      },
    ],
    accounting_policy:
      "Only independently verified external mainnet or fiat USD revenue counts. Trial credits, sandbox credits, testnet, first-party canaries, unverified payers, and self-funded transactions are excluded.",
  };
}

// Image security config. SVG sources with .svg extension auto-skip the
// optimization endpoint on the client side (served directly, no proxy).
// To route SVGs through the optimizer (with security headers), set
// dangerouslyAllowSVG: true in next.config.js and uncomment below:
// const imageConfig: ImageConfig = { dangerouslyAllowSVG: true };

const worker = {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/.well-known/worker-signing-key.json") {
      return Response.json(
        signingKeyDocument(url.origin, env.WORKER_ED25519_PUBLIC_JWK_JSON),
        {
          headers: {
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=300, s-maxage=300",
          },
        },
      );
    }

    if (url.pathname === "/.well-known/jwks.json") {
      return Response.json(
        signingJwksDocument(env.WORKER_ED25519_PUBLIC_JWK_JSON),
        {
          headers: {
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=300, s-maxage=300",
          },
        },
      );
    }

    if (url.pathname === "/api/signed-agent-guild-preflight") {
      return handleSignedPreflight(
        request,
        url.origin,
        env.WORKER_ED25519_PRIVATE_KEY_PKCS8_B64,
        env.WORKER_ED25519_PUBLIC_JWK_JSON,
      );
    }

    if (
      url.pathname === "/.well-known/agent-card.json" ||
      url.pathname === "/.well-known/agent.json"
    ) {
      const card = await signAgentCard(
        agentCard(url.origin),
        url.origin,
        env.WORKER_ED25519_PRIVATE_KEY_PKCS8_B64,
        env.WORKER_ED25519_PUBLIC_JWK_JSON,
      );
      return Response.json(card, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Cache-Control": "public, max-age=300, s-maxage=300",
        },
      });
    }

    if (url.pathname === "/llms.txt") {
      return new Response(llmsText(url.origin), {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Cache-Control": "public, max-age=300, s-maxage=300",
          "Content-Type": "text/plain; charset=utf-8",
        },
      });
    }

    if (url.pathname === "/commerce.json") {
      return Response.json(commerceCatalog(url.origin), {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Cache-Control": "public, max-age=300, s-maxage=300",
        },
      });
    }

    if (
      url.pathname === "/openapi.json" ||
      url.pathname === "/.well-known/agent-guild-commerce-openapi.json"
    ) {
      return Response.json(agentGuildCommerceOpenApi(url.origin), {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Cache-Control": "public, max-age=300, s-maxage=300",
        },
      });
    }

    if (url.pathname === "/.well-known/x402" && request.method === "GET") {
      const manifest = await x402Manifest(url.origin);
      return Response.json(manifest, {
        status: manifest.count > 0 ? 200 : 503,
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Cache-Control": "public, max-age=60, s-maxage=60, stale-while-revalidate=300",
        },
      });
    }

    if (
      url.pathname === "/trust-decision" ||
      url.pathname === "/preflight/deep" ||
      url.pathname === "/evidence/bundle" ||
      url.pathname === "/envelopes/issue"
    ) {
      if (request.method === "OPTIONS") return relayOptions();

      let canonical: URL | null = null;
      if (url.pathname === "/trust-decision" && request.method === "GET") {
        const capability = url.searchParams.get("capability") ?? "fact-check";
        const supported = DISCOVERY_CAPABILITIES.some(({ id }) => id === capability);
        if (!supported) {
          return Response.json(
            {
              error: "unsupported_capability",
              supported: DISCOVERY_CAPABILITIES.map(({ id }) => id),
            },
            { status: 422, headers: relayCorsHeaders() },
          );
        }
        canonical = new URL(trustCheckUrl(capability));
      } else if (
        (url.pathname === "/preflight/deep" && request.method === "GET") ||
        (url.pathname === "/evidence/bundle" && request.method === "POST") ||
        (url.pathname === "/envelopes/issue" && request.method === "POST")
      ) {
        canonical = new URL(url.pathname + url.search, GUILD_BASE);
      }

      if (!canonical) {
        return Response.json(
          { error: "method_not_allowed" },
          {
            status: 405,
            headers: {
              ...relayCorsHeaders(),
              Allow:
                url.pathname === "/trust-decision" || url.pathname === "/preflight/deep"
                  ? "GET, OPTIONS"
                  : "POST, OPTIONS",
            },
          },
        );
      }
      return relayAgentGuild(request, canonical);
    }

    if (url.pathname === "/robots.txt") {
      return new Response(
        `User-agent: *\nAllow: /\nSitemap: ${url.origin}/sitemap.xml\n`,
        {
          headers: {
            "Cache-Control": "public, max-age=3600, s-maxage=3600",
            "Content-Type": "text/plain; charset=utf-8",
          },
        },
      );
    }

    if (url.pathname === "/sitemap.xml") {
      const pages = [
        url.origin,
        `${url.origin}/.well-known/agent-card.json`,
        `${url.origin}/.well-known/jwks.json`,
        `${url.origin}/.well-known/worker-signing-key.json`,
        `${url.origin}/.well-known/x402`,
        `${url.origin}/commerce.json`,
        `${url.origin}/openapi.json`,
        `${url.origin}/llms.txt`,
      ];
      const xml = `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${pages
        .map((page) => `  <url><loc>${page}</loc></url>`)
        .join("\n")}\n</urlset>\n`;
      return new Response(xml, {
        headers: {
          "Cache-Control": "public, max-age=3600, s-maxage=3600",
          "Content-Type": "application/xml; charset=utf-8",
        },
      });
    }

    if (url.pathname === "/health") {
      return Response.json({
        status: "ok",
        agent_id: AGENT_ID,
        a2a: `${url.origin}/a2a`,
      });
    }

    if (url.pathname === "/_vinext/image") {
      const allowedWidths = [...DEFAULT_DEVICE_SIZES, ...DEFAULT_IMAGE_SIZES];
      return handleImageOptimization(request, {
        fetchAsset: (path) => env.ASSETS.fetch(new Request(new URL(path, request.url))),
        transformImage: async (body, { width, format, quality }) => {
          const result = await env.IMAGES.input(body).transform(width > 0 ? { width } : {}).output({ format, quality });
          return result.response();
        },
      }, allowedWidths);
    }

    return handler.fetch(request, env, ctx);
  },
};

export default worker;
