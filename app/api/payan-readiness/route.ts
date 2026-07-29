import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const PAYAN_BASE = "https://payanagent.com";
const OFFER_ID_PATTERN = /^kh[a-z0-9]{20,60}$/;
const REQUEST_TIMEOUT_MS = 8_000;

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Cache-Control": "no-store",
};

type PayanOffer = {
  _id?: string;
  title?: string;
  category?: string;
  description?: string;
  httpMethod?: string;
  isActive?: boolean;
  offerType?: string;
  priceCents?: number;
  priceUsd?: number;
  sellerId?: string;
  tags?: string[];
};

function json(body: unknown, status = 200) {
  return NextResponse.json(body, { status, headers: corsHeaders });
}

function safeJson(text: string) {
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text.slice(0, 2_000);
  }
}

function verdictFor(offer: PayanOffer, challengeStatus: number) {
  if (offer.isActive === false) {
    return {
      status: "unavailable",
      reason: "The marketplace marks this offer inactive.",
    };
  }
  if (challengeStatus === 402) {
    return {
      status: "ready",
      reason:
        "The public buy surface is reachable and returned the expected unpaid x402 challenge.",
    };
  }
  if (challengeStatus >= 500 || challengeStatus === 0) {
    return {
      status: "unavailable",
      reason:
        "The public buy surface failed before returning an x402 payment challenge.",
    };
  }
  return {
    status: "caution",
    reason: `Expected HTTP 402 from the unpaid buy surface, received HTTP ${challengeStatus}.`,
  };
}

export function GET(request: NextRequest) {
  return json({
    service: "PayanAgent x402 offer readiness",
    version: "1.0.0",
    method: "POST",
    endpoint: new URL("/api/payan-readiness", request.url).toString(),
    input: {
      offerId: "PayanAgent offer id beginning with kh",
    },
    output:
      "Public offer metadata, unpaid x402 challenge status, latency, and a ready/caution/unavailable verdict.",
    safety:
      "Only payanagent.com public offer and buy surfaces are queried. No payment is signed or sent.",
  });
}

export function OPTIONS() {
  return new NextResponse(null, { status: 204, headers: corsHeaders });
}

export async function POST(request: NextRequest) {
  let body: { offerId?: unknown };
  try {
    body = (await request.json()) as { offerId?: unknown };
  } catch {
    return json(
      {
        error: "invalid_json",
        message: "Expected JSON with a PayanAgent offerId.",
      },
      400,
    );
  }

  const offerId =
    typeof body.offerId === "string" ? body.offerId.trim() : "";
  if (!OFFER_ID_PATTERN.test(offerId)) {
    return json(
      {
        error: "invalid_offer_id",
        message:
          "offerId must be a PayanAgent offer id beginning with kh and containing only lowercase letters and digits.",
      },
      400,
    );
  }

  const detailUrl = `${PAYAN_BASE}/api/v1/offers/${offerId}`;
  const buyUrl = `${PAYAN_BASE}/x402/${offerId}`;

  let detailResponse: Response;
  const detailStartedAt = Date.now();
  try {
    detailResponse = await fetch(detailUrl, {
      headers: { accept: "application/json" },
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch (error) {
    return json(
      {
        offerId,
        checkedAt: new Date().toISOString(),
        verdict: {
          status: "unavailable",
          reason: "The public PayanAgent offer API was unreachable.",
        },
        error: error instanceof Error ? error.message : "Offer lookup failed.",
      },
      502,
    );
  }

  const detailLatencyMs = Date.now() - detailStartedAt;
  const detailText = await detailResponse.text();
  if (!detailResponse.ok) {
    return json(
      {
        offerId,
        checkedAt: new Date().toISOString(),
        offerLookup: {
          url: detailUrl,
          httpStatus: detailResponse.status,
          latencyMs: detailLatencyMs,
          response: safeJson(detailText),
        },
        verdict: {
          status: "unavailable",
          reason: "The marketplace did not return an active public offer record.",
        },
      },
      detailResponse.status === 404 ? 404 : 502,
    );
  }

  const detailPayload = safeJson(detailText) as
    | { offer?: PayanOffer }
    | null;
  const offer = detailPayload?.offer;
  if (!offer) {
    return json(
      {
        offerId,
        checkedAt: new Date().toISOString(),
        verdict: {
          status: "unavailable",
          reason: "The marketplace response did not contain an offer object.",
        },
      },
      502,
    );
  }

  let challengeStatus = 0;
  let challengeLatencyMs = 0;
  let challengeBody: unknown = null;
  let paymentRequiredHeader: string | null = null;
  try {
    const challengeStartedAt = Date.now();
    const challengeResponse = await fetch(buyUrl, {
      method: offer.httpMethod?.toUpperCase() === "GET" ? "GET" : "POST",
      headers: {
        accept: "application/json",
        "content-type": "application/json",
      },
      body:
        offer.httpMethod?.toUpperCase() === "GET"
          ? undefined
          : JSON.stringify({}),
      redirect: "manual",
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    challengeLatencyMs = Date.now() - challengeStartedAt;
    challengeStatus = challengeResponse.status;
    paymentRequiredHeader = challengeResponse.headers.get("payment-required");
    challengeBody = safeJson(await challengeResponse.text());
  } catch (error) {
    challengeBody = {
      error: error instanceof Error ? error.message : "Buy-surface probe failed.",
    };
  }

  return json({
    offerId,
    checkedAt: new Date().toISOString(),
    offer: {
      id: offer._id ?? offerId,
      title: offer.title ?? null,
      description: offer.description ?? null,
      category: offer.category ?? null,
      tags: offer.tags ?? [],
      sellerId: offer.sellerId ?? null,
      offerType: offer.offerType ?? null,
      httpMethod: offer.httpMethod ?? null,
      isActive: offer.isActive ?? null,
      priceCents: offer.priceCents ?? null,
      priceUsd: offer.priceUsd ?? null,
      lookupLatencyMs: detailLatencyMs,
    },
    unpaidChallenge: {
      url: buyUrl,
      httpStatus: challengeStatus,
      latencyMs: challengeLatencyMs,
      paymentRequired: challengeStatus === 402,
      paymentRequiredHeaderPresent: Boolean(paymentRequiredHeader),
      response: challengeBody,
    },
    verdict: verdictFor(offer, challengeStatus),
    safety: {
      paidCallMade: false,
      paymentSigned: false,
      hostsQueried: ["payanagent.com"],
    },
  });
}
