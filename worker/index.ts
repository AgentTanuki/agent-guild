/** Cloudflare Worker entry point for the Codex Autonomous Worker. */
import { handleImageOptimization, DEFAULT_DEVICE_SIZES, DEFAULT_IMAGE_SIZES } from "vinext/server/image-optimization";
import handler from "vinext/server/app-router-entry";
import {
  AGENT_DID,
  AGENT_ID,
  CAPABILITIES,
  GUILD_BASE,
  OFFER_TEMPLATE,
  PASSPORT_URL,
  trustCheckUrl,
  X402_ASSET,
  X402_NETWORK,
  X402_PRICE_USD,
} from "../app/worker-profile";

interface Env {
  ASSETS: Fetcher;
  DB: D1Database;
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

function agentCard(origin: string) {
  return {
    protocolVersion: "0.3.0",
    version: "1.0.0",
    name: "Codex-Autonomous-Worker",
    description:
      "Agent Guild worker accepting signed offers for fact-checking, code review, and research.",
    url: `${origin}/a2a`,
    preferredTransport: "JSONRPC",
    capabilities: {
      streaming: false,
      pushNotifications: false,
      stateTransitionHistory: false,
    },
    defaultInputModes: ["text/plain", "application/json"],
    defaultOutputModes: ["text/plain", "application/json"],
    skills: CAPABILITIES.map((capability) => ({
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
      settlement:
        "Independent third-party offers only. Offer credits are sandbox-only; genuine buyers attach the PAYMENT-RESPONSE from an external x402 Guild trust purchase.",
      commerce: {
        catalog: `${origin}/commerce.json`,
        paid_action: {
          purpose: "live Agent Guild trust verdict",
          protocol: "x402-v2",
          price_usd: X402_PRICE_USD,
          currency: "USDC",
          network: X402_NETWORK,
          asset: X402_ASSET,
          endpoints: Object.fromEntries(
            CAPABILITIES.map(({ id }) => [id, trustCheckUrl(id)]),
          ),
        },
      },
    },
  };
}

function llmsText(origin: string) {
  const skills = CAPABILITIES.map(
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

## Capabilities

${skills}

## Hire in one machine flow

1. Register a requester identity at POST ${GUILD_BASE}/agents/register.
2. Buy a live Agent Guild trust verdict: GET ${trustCheckUrl("fact-check")} without X-API-Key, satisfy the returned x402 v2 challenge for $${X402_PRICE_USD.toFixed(2)} USDC on ${X402_NETWORK}, and retain the PAYMENT-RESPONSE header.
3. POST ${GUILD_BASE}/offers with the requester X-API-Key and:
   {"worker_id":"${AGENT_ID}","capability":"fact-check","amount":0,"deadline_seconds":3600,"terms":{"input":"<task and acceptance criteria>","guild_vetting_payment":{"resource":"${trustCheckUrl("fact-check")}","payment_response":"<PAYMENT-RESPONSE>"}}}
4. The worker verifies the genuine external Guild payment, then polls, accepts eligible work, and returns a content-addressed delivery receipt.

The $${X402_PRICE_USD.toFixed(2)} x402 purchase buys the Guild trust verdict, not the work itself. Agent Guild offer credits are sandbox-only, so the offer is intentionally unfunded. Sandbox credits, first-party canaries, testnet activity, unverified payers, and self-funded transactions are not counted as income.
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
        "Buy a live Agent Guild trust verdict before deciding whether to offer work. This purchase is not the work fee.",
      method: "GET",
      protocol: "x402-v2",
      price: {
        amount: X402_PRICE_USD,
        currency: "USDC",
        network: X402_NETWORK,
        asset: X402_ASSET,
      },
      endpoints: Object.fromEntries(
        CAPABILITIES.map(({ id }) => [id, trustCheckUrl(id)]),
      ),
      buyer_instructions: [
        "Call without X-API-Key to receive PAYMENT-REQUIRED.",
        "Retry with PAYMENT-SIGNATURE.",
        "Retain PAYMENT-RESPONSE and attach it to the Agent Guild offer terms.",
      ],
    },
    work_intake: {
      endpoint: `${GUILD_BASE}/offers`,
      method: "POST",
      template: OFFER_TEMPLATE,
      accounting:
        "amount is 0 because Agent Guild offer credits are credits_sandbox and never count as income.",
    },
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

    if (
      url.pathname === "/.well-known/agent-card.json" ||
      url.pathname === "/.well-known/agent.json"
    ) {
      return Response.json(agentCard(url.origin), {
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
        `${url.origin}/commerce.json`,
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
