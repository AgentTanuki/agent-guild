import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const GUILD_BASE = "https://agent-guild-5d5r.onrender.com";
const REQUEST_TIMEOUT_MS = 12_000;

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Cache-Control": "no-store",
};

function json(body: unknown, status = 200) {
  return NextResponse.json(body, { status, headers: corsHeaders });
}

function parseTarget(value: unknown) {
  if (typeof value !== "string" || value.length > 2_048) return null;
  try {
    const url = new URL(value.trim());
    if (
      !["http:", "https:"].includes(url.protocol) ||
      url.username ||
      url.password
    ) {
      return null;
    }
    return url.toString();
  } catch {
    return null;
  }
}

function safeJson(text: string) {
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return { raw: text.slice(0, 2_000) };
  }
}

export function GET(request: NextRequest) {
  return json({
    service: "Agent Guild endpoint preflight adapter",
    version: "1.0.0",
    method: "POST",
    endpoint: new URL("/api/agent-guild-preflight", request.url).toString(),
    input: { url: "https://public-agent.example/a2a" },
    output:
      "Agent Guild live identity, protocol, liveness, and delegation verdict for one public endpoint.",
    freeAlternative: `${GUILD_BASE}/preflight?url=<url>`,
    commerce:
      "The Agent Guild upstream check is free. A PayanAgent purchase pays only for this POST-compatible adapter and its public signed marketplace receipt.",
    safety:
      "The adapter forwards only one HTTP(S) URL to Agent Guild's SSRF-safe preflight service. It accepts no credentials and makes no paid call.",
  });
}

export function OPTIONS() {
  return new NextResponse(null, { status: 204, headers: corsHeaders });
}

export async function POST(request: NextRequest) {
  let body: { url?: unknown };
  try {
    body = (await request.json()) as { url?: unknown };
  } catch {
    return json(
      {
        error: "invalid_json",
        message: "Expected JSON with one public HTTP(S) url.",
      },
      400,
    );
  }

  const target = parseTarget(body.url);
  if (!target) {
    return json(
      {
        error: "invalid_url",
        message:
          "url must be an absolute public HTTP(S) URL without embedded credentials.",
      },
      400,
    );
  }

  const upstream = `${GUILD_BASE}/preflight?url=${encodeURIComponent(target)}`;
  const startedAt = Date.now();
  let response: Response;
  try {
    response = await fetch(upstream, {
      headers: { accept: "application/json" },
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch (error) {
    return json(
      {
        target,
        checkedAt: new Date().toISOString(),
        error: "upstream_unavailable",
        message:
          error instanceof Error
            ? error.message
            : "Agent Guild preflight was unreachable.",
      },
      502,
    );
  }

  const result = safeJson(await response.text());
  const payload = {
    target,
    checkedAt: new Date().toISOString(),
    latencyMs: Date.now() - startedAt,
    result,
    source: {
      provider: "Agent Guild",
      upstream,
      upstreamHttpStatus: response.status,
      freeAlternative: upstream,
    },
    adapter: {
      paidCallMade: false,
      credentialsAccepted: false,
      claim:
        "A PayanAgent receipt proves purchase of this adapter call; it does not change or strengthen Agent Guild's verdict.",
    },
  };

  if (!response.ok) {
    return json(payload, response.status >= 500 ? 502 : response.status);
  }
  return json(payload);
}
