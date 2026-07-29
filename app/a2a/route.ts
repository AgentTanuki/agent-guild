import { NextRequest, NextResponse } from "next/server";
import {
  AGENT_DID,
  AGENT_ID,
  DISCOVERY_CAPABILITIES,
  GUILD_BASE,
  OFFER_TEMPLATE,
  PASSPORT_URL,
  trustCheckUrl,
  X402_ASSET,
  X402_NETWORK,
  X402_PRICE_USD,
} from "../worker-profile";

export const dynamic = "force-dynamic";

type JsonRpcRequest = {
  jsonrpc?: string;
  id?: string | number | null;
  method?: string;
  params?: {
    message?: {
      parts?: Array<{ kind?: string; text?: string }>;
    };
  };
};

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Content-Type, X-API-Key",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Cache-Control": "no-store",
};

function json(body: unknown, status = 200) {
  return NextResponse.json(body, { status, headers: corsHeaders });
}

export function GET() {
  return json({
    status: "online",
    protocol: "A2A JSON-RPC 0.3.0",
    method: "message/send",
    agent_id: AGENT_ID,
    did: AGENT_DID,
    passport: PASSPORT_URL,
    capabilities: DISCOVERY_CAPABILITIES.map(({ id }) => id),
  });
}

export function OPTIONS() {
  return new NextResponse(null, { status: 204, headers: corsHeaders });
}

export async function POST(request: NextRequest) {
  let body: JsonRpcRequest;
  try {
    body = (await request.json()) as JsonRpcRequest;
  } catch {
    return json({
      jsonrpc: "2.0",
      id: null,
      error: { code: -32700, message: "Parse error: expected JSON." },
    });
  }

  const id = body.id ?? null;
  if (body.jsonrpc !== "2.0" || body.method !== "message/send") {
    return json({
      jsonrpc: "2.0",
      id,
      error: {
        code: -32601,
        message: "Method not found. This endpoint implements message/send.",
      },
    });
  }

  const parts = body.params?.message?.parts;
  const text = Array.isArray(parts)
    ? parts
        .filter((part) => part?.kind === "text" && typeof part.text === "string")
        .map((part) => part.text)
        .join(" ")
        .trim()
    : "";

  if (!text) {
    return json({
      jsonrpc: "2.0",
      id,
      error: {
        code: -32602,
        message: "Invalid params: include at least one text part.",
      },
    });
  }

  const normalized = text.toLowerCase();
  const requestedCapability = DISCOVERY_CAPABILITIES.find(({ id: capabilityId }) =>
    normalized.includes(capabilityId),
  );
  const capability = requestedCapability?.id ?? OFFER_TEMPLATE.capability;
  const vettingUrl = trustCheckUrl(capability);
  const response = {
    kind: "signed_offer_intake",
    status: "ready",
    agent: {
      agent_id: AGENT_ID,
      did: AGENT_DID,
      passport: PASSPORT_URL,
    },
    requested_capability: requestedCapability?.id ?? null,
    capabilities: DISCOVERY_CAPABILITIES.map(
      ({ id: capabilityId }) => capabilityId,
    ),
    paid_action: {
      purpose:
        "Buy a live Agent Guild trust verdict before offering work. This is a Guild service purchase, not a payment for the work.",
      call: `GET ${vettingUrl}`,
      protocol: "x402-v2",
      price: {
        amount: X402_PRICE_USD,
        currency: "USDC",
        network: X402_NETWORK,
        asset: X402_ASSET,
      },
      authentication:
        "Omit X-API-Key so the endpoint returns an x402 challenge. Retry with PAYMENT-SIGNATURE and retain PAYMENT-RESPONSE.",
    },
    next_action: {
      call: `POST ${GUILD_BASE}/offers`,
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": "<requester API key>",
      },
      body: {
        ...OFFER_TEMPLATE,
        capability,
        terms: {
          ...OFFER_TEMPLATE.terms,
          guild_vetting_payment: {
            resource: vettingUrl,
            payment_response:
              "<PAYMENT-RESPONSE header from the requester's external x402 purchase>",
          },
        },
      },
    },
    policy: {
      work: "Eligible third-party offers are polled and fulfilled autonomously.",
      settlement:
        "The offer is intentionally unfunded because Agent Guild offer credits are sandbox-only. The external x402 trust purchase is independently verified before work is accepted.",
      accounting:
        "Only independently verified external fiat or mainnet settlement counts as income.",
    },
  };

  return json({
    jsonrpc: "2.0",
    id,
    result: {
      kind: "message",
      role: "agent",
      messageId: `codex-${String(id ?? "response")}`,
      parts: [{ kind: "text", text: JSON.stringify(response) }],
    },
  });
}
