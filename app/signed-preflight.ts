import { AGENT_DID, AGENT_ID, GUILD_BASE } from "./worker-profile";

const REQUEST_TIMEOUT_MS = 12_000;
const MAX_FRESHNESS_SECONDS = 300;
const KEY_FRAGMENT = "ed25519-worker-1";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Cache-Control": "no-store",
};

type PublicJwk = {
  kty: "OKP";
  crv: "Ed25519";
  x: string;
};

type SignedPreflightInput = {
  url?: unknown;
  recipient?: unknown;
  nonce?: unknown;
  purpose?: unknown;
};

function json(body: unknown, status = 200) {
  return Response.json(body, { status, headers: corsHeaders });
}

function parsePublicJwk(value?: string): PublicJwk | null {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as Partial<PublicJwk>;
    if (
      parsed.kty !== "OKP" ||
      parsed.crv !== "Ed25519" ||
      typeof parsed.x !== "string" ||
      parsed.x.length < 32
    ) {
      return null;
    }
    return parsed as PublicJwk;
  } catch {
    return null;
  }
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

function boundedString(
  value: unknown,
  minimum: number,
  maximum: number,
) {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  if (normalized.length < minimum || normalized.length > maximum) return null;
  return normalized;
}

function canonicalize(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("non-finite number");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalize(item)).join(",")}]`;
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    const keys = Object.keys(record)
      .filter((key) => record[key] !== undefined)
      .sort();
    return `{${keys
      .map((key) => `${JSON.stringify(key)}:${canonicalize(record[key])}`)
      .join(",")}}`;
  }
  throw new Error("unsupported JSON value");
}

function base64Url(bytes: ArrayBuffer | Uint8Array) {
  let binary = "";
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  for (const byte of view) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/u, "");
}

function decodeBase64(value: string) {
  const binary = atob(value);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

async function fetchJson(url: string) {
  const response = await fetch(url, {
    headers: { accept: "application/json" },
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  const text = await response.text();
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    body = { raw: text.slice(0, 2_000) };
  }
  return { response, body };
}

export function signingKeyDocument(origin: string, publicJwkJson?: string) {
  const publicKeyJwk = parsePublicJwk(publicJwkJson);
  return {
    schema: "agent-guild/worker-signing-key/v1",
    id: `${origin}/.well-known/worker-signing-key.json#${KEY_FRAGMENT}`,
    controller: origin,
    agent_guild_identity: {
      agent_id: AGENT_ID,
      did: AGENT_DID,
      note:
        "The Agent Guild identity is the worker being referenced. This signing key is controlled by the worker site and is not an Agent Guild issuer key.",
    },
    publicKeyJwk,
    algorithm: "Ed25519",
    canonicalization: "RFC 8785 JSON Canonicalization Scheme",
    configured: Boolean(publicKeyJwk),
  };
}

export function signingJwksDocument(publicJwkJson?: string) {
  const publicKeyJwk = parsePublicJwk(publicJwkJson);
  return {
    keys: publicKeyJwk
      ? [
          {
            ...publicKeyJwk,
            kid: KEY_FRAGMENT,
            alg: "EdDSA",
            use: "sig",
            key_ops: ["verify"],
          },
        ]
      : [],
  };
}

export async function signAgentCard<T extends Record<string, unknown>>(
  card: T,
  origin: string,
  privateKeyPkcs8Base64?: string,
  publicJwkJson?: string,
) {
  const publicJwk = parsePublicJwk(publicJwkJson);
  if (!privateKeyPkcs8Base64 || !publicJwk) return card;

  try {
    const privateKey = await crypto.subtle.importKey(
      "pkcs8",
      decodeBase64(privateKeyPkcs8Base64),
      { name: "Ed25519" },
      false,
      ["sign"],
    );
    const protectedHeader = base64Url(
      new TextEncoder().encode(
        JSON.stringify({
          alg: "EdDSA",
          typ: "JOSE",
          kid: KEY_FRAGMENT,
          jku: `${origin}/.well-known/jwks.json`,
        }),
      ),
    );
    const payload = base64Url(
      new TextEncoder().encode(canonicalize(card)),
    );
    const signature = await crypto.subtle.sign(
      "Ed25519",
      privateKey,
      new TextEncoder().encode(`${protectedHeader}.${payload}`),
    );
    return {
      ...card,
      signatures: [
        {
          protected: protectedHeader,
          signature: base64Url(signature),
          header: {
            jwk: {
              ...publicJwk,
              kid: KEY_FRAGMENT,
              alg: "EdDSA",
              use: "sig",
              key_ops: ["verify"],
            },
          },
        },
      ],
    };
  } catch {
    return card;
  }
}

export function signedPreflightDescription(origin: string) {
  return {
    service: "Caller-bound signed Agent Guild preflight snapshot",
    version: "1.0.0",
    method: "POST",
    endpoint: `${origin}/api/signed-agent-guild-preflight`,
    input: {
      url: "https://public-agent.example/a2a",
      recipient: "did:key:buyer-or-other-stable-machine-identifier",
      nonce: "caller-generated-unique-nonce",
      purpose: "pre-delegation endpoint trust",
    },
    output:
      "A five-minute worker-signed packet binding the caller, nonce, target endpoint, exact Agent Guild preflight result, and deployed Guild release.",
    verificationKey: `${origin}/.well-known/worker-signing-key.json`,
    freeAlternative: `${GUILD_BASE}/preflight?url=<url>`,
    issuerBoundary:
      "The packet is signed by Codex-Autonomous-Worker, not by Agent Guild. The embedded Agent Guild result and release are source-attributed observations.",
    paymentBoundary:
      "A marketplace purchase pays for the caller-bound signed snapshot. Agent Guild's underlying live preflight remains free.",
  };
}

export function signedPreflightOptions() {
  return new Response(null, { status: 204, headers: corsHeaders });
}

export async function handleSignedPreflight(
  request: Request,
  origin: string,
  privateKeyPkcs8Base64?: string,
  publicJwkJson?: string,
) {
  if (request.method === "GET") return json(signedPreflightDescription(origin));
  if (request.method === "OPTIONS") return signedPreflightOptions();
  if (request.method !== "POST") {
    return json({ error: "method_not_allowed" }, 405);
  }

  let input: SignedPreflightInput;
  try {
    input = (await request.json()) as SignedPreflightInput;
  } catch {
    return json({ error: "invalid_json" }, 400);
  }

  const target = parseTarget(input.url);
  const recipient = boundedString(input.recipient, 3, 512);
  const nonce = boundedString(input.nonce, 8, 256);
  const purpose = boundedString(
    input.purpose ?? "pre-delegation endpoint trust",
    3,
    256,
  );
  if (!target || !recipient || !nonce || !purpose) {
    return json(
      {
        error: "invalid_input",
        message:
          "url, recipient, nonce (at least 8 characters), and purpose must be bounded strings; url must be absolute HTTP(S) without credentials.",
      },
      400,
    );
  }

  const publicJwk = parsePublicJwk(publicJwkJson);
  if (!privateKeyPkcs8Base64 || !publicJwk) {
    return json(
      {
        error: "signing_unavailable",
        message: "The worker signing identity is not fully configured.",
      },
      503,
    );
  }

  const preflightUrl = `${GUILD_BASE}/preflight?url=${encodeURIComponent(target)}`;
  const releaseUrl = `${GUILD_BASE}/release`;
  let preflight: Awaited<ReturnType<typeof fetchJson>>;
  let release: Awaited<ReturnType<typeof fetchJson>>;
  try {
    [preflight, release] = await Promise.all([
      fetchJson(preflightUrl),
      fetchJson(releaseUrl),
    ]);
  } catch (error) {
    return json(
      {
        error: "upstream_unavailable",
        message:
          error instanceof Error ? error.message : "Agent Guild was unreachable.",
      },
      502,
    );
  }
  if (!preflight.response.ok || !release.response.ok) {
    return json(
      {
        error: "upstream_failed",
        preflight_http_status: preflight.response.status,
        release_http_status: release.response.status,
      },
      502,
    );
  }

  const issuedAt = new Date();
  const expiresAt = new Date(
    issuedAt.getTime() + MAX_FRESHNESS_SECONDS * 1_000,
  );
  const verificationMethod = `${origin}/.well-known/worker-signing-key.json#${KEY_FRAGMENT}`;
  const payload = {
    type: "agent-guild/caller-bound-preflight-snapshot/v1",
    issuer: {
      id: origin,
      agent_id: AGENT_ID,
      verification_method: verificationMethod,
      boundary: "worker-signed; not Agent-Guild-signed",
    },
    recipient,
    nonce,
    purpose,
    subject: { endpoint: target },
    issued_at: issuedAt.toISOString(),
    expires_at: expiresAt.toISOString(),
    agent_guild: {
      preflight_source: preflightUrl,
      release_source: releaseUrl,
      release: release.body,
      result: preflight.body,
    },
  };

  let signature: ArrayBuffer;
  try {
    const privateKey = await crypto.subtle.importKey(
      "pkcs8",
      decodeBase64(privateKeyPkcs8Base64),
      { name: "Ed25519" },
      false,
      ["sign"],
    );
    signature = await crypto.subtle.sign(
      "Ed25519",
      privateKey,
      new TextEncoder().encode(canonicalize(payload)),
    );
  } catch {
    return json(
      {
        error: "signing_failed",
        message: "The snapshot was not issued because signing failed.",
      },
      503,
    );
  }

  return json({
    payload,
    proof: {
      type: "Ed25519JcsSignature2026",
      algorithm: "Ed25519",
      canonicalization: "RFC8785-JCS",
      verification_method: verificationMethod,
      signature_base64url: base64Url(signature),
    },
    verification: {
      public_key: `${origin}/.well-known/worker-signing-key.json`,
      steps: [
        "Remove proof and canonicalize payload using RFC 8785 JCS.",
        "Decode proof.signature_base64url and verify Ed25519 over the canonical payload.",
        "Reject after payload.expires_at or if recipient, nonce, purpose, or endpoint differs from the caller's request.",
        "Treat payload.agent_guild.result as a worker-observed Agent Guild response; verify the named Guild release independently when assurance requires it.",
      ],
    },
  });
}
