import { GUILD_BASE } from "./worker-profile";
import { signAgentCard } from "./signed-preflight";

const BASE_CHAIN_ID = 8453;
const BASE_RPCS = [
  "https://mainnet.base.org",
  "https://base-rpc.publicnode.com",
  "https://base-mainnet.public.blastapi.io",
  "https://1rpc.io/base",
] as const;
const IDENTITY_REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432";
const AGENT_REGISTRY = `eip155:${BASE_CHAIN_ID}:${IDENTITY_REGISTRY}`;
const OWNER_OF_SELECTOR = "6352211e";
const TOKEN_URI_SELECTOR = "c87b56dd";
const GET_AGENT_WALLET_SELECTOR = "00339509";
const RPC_TIMEOUT_MS = 10_000;
const REGISTRATION_TIMEOUT_MS = 10_000;
const MAX_REGISTRATION_BYTES = 131_072;
const SERVICE_PRIORITY = ["a2a", "mcp", "web"];

type Registration = {
  type?: unknown;
  name?: unknown;
  description?: unknown;
  services?: unknown;
  x402Support?: unknown;
  active?: unknown;
  registrations?: unknown;
  supportedTrust?: unknown;
};

type Service = {
  name: string;
  endpoint: string;
  version?: string;
};

export type Erc8004Resolution = {
  schema: "agent-guild/erc8004-resolution/v1";
  chain: {
    namespace: "eip155";
    chain_id: 8453;
    rpc: string;
  };
  identity_registry: string;
  agent_registry: string;
  agent_id: string;
  owner: string;
  agent_wallet: string | null;
  agent_uri: string;
  registration: {
    type: string | null;
    name: string | null;
    description: string | null;
    active: boolean | null;
    x402_support: boolean | null;
    supported_trust: string[];
    registration_matches_onchain_identity: true;
  };
  service: Service;
  endpoint_domain_verification: {
    status: "registration-origin" | "well-known";
    verification_url: string;
  };
  resolved_at: string;
};

export type Erc8004ResolutionResult =
  | { ok: true; value: Erc8004Resolution }
  | { ok: false; status: number; error: string; message: string };

function failure(
  status: number,
  error: string,
  message: string,
): Erc8004ResolutionResult {
  return { ok: false, status, error, message };
}

function parseAgentId(value: string | null) {
  if (!value || !/^(0|[1-9][0-9]{0,77})$/u.test(value)) return null;
  try {
    const id = BigInt(value);
    if (id >= 2n ** 256n) return null;
    return { decimal: id.toString(), hex: id.toString(16).padStart(64, "0") };
  } catch {
    return null;
  }
}

function parseAddress(result: string) {
  if (!/^0x[0-9a-fA-F]{64}$/u.test(result)) return null;
  return `0x${result.slice(-40).toLowerCase()}`;
}

function decodeAbiString(result: string) {
  if (!/^0x[0-9a-fA-F]+$/u.test(result) || result.length < 130) return null;
  try {
    const hex = result.slice(2);
    const offset = Number(BigInt(`0x${hex.slice(0, 64)}`));
    const lengthOffset = offset * 2;
    const length = Number(BigInt(`0x${hex.slice(lengthOffset, lengthOffset + 64)}`));
    if (!Number.isSafeInteger(length) || length < 1 || length > MAX_REGISTRATION_BYTES) {
      return null;
    }
    const start = lengthOffset + 64;
    const encoded = hex.slice(start, start + length * 2);
    if (encoded.length !== length * 2) return null;
    const bytes = Uint8Array.from(encoded.match(/.{2}/gu) ?? [], (byte) =>
      Number.parseInt(byte, 16),
    );
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return null;
  }
}

async function ethCalls(data: string[]) {
  let lastError = "No Base RPC returned a result.";
  for (const rpc of BASE_RPCS) {
    try {
      const response = await fetch(rpc, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(
          data.map((callData, index) => ({
            jsonrpc: "2.0",
            id: index + 1,
            method: "eth_call",
            params: [
              { to: IDENTITY_REGISTRY, data: `0x${callData}` },
              "latest",
            ],
          })),
        ),
        signal: AbortSignal.timeout(RPC_TIMEOUT_MS),
      });
      if (!response.ok) {
        lastError = `${new URL(rpc).hostname} returned HTTP ${response.status}`;
        continue;
      }
      const body = (await response.json()) as Array<{
        id?: unknown;
        result?: unknown;
        error?: { message?: unknown };
      }>;
      if (!Array.isArray(body)) {
        lastError = `${new URL(rpc).hostname} rejected JSON-RPC batching`;
        continue;
      }
      const ordered = [...body].sort((left, right) => Number(left.id) - Number(right.id));
      if (
        ordered.length !== data.length ||
        ordered.some((entry) => typeof entry.result !== "string")
      ) {
        const message = ordered.find((entry) => typeof entry.error?.message === "string")
          ?.error?.message;
        lastError = typeof message === "string" ? message : `${new URL(rpc).hostname} returned incomplete results`;
        continue;
      }
      return {
        rpc,
        results: ordered.map((entry) => entry.result as string),
      };
    } catch (error) {
      lastError = error instanceof Error ? error.message : "Base RPC request failed";
    }
  }
  throw new Error(lastError);
}

function isUnsafeHostname(hostname: string) {
  const normalized = hostname.toLowerCase().replace(/\.$/u, "");
  if (
    normalized === "localhost" ||
    normalized.endsWith(".localhost") ||
    normalized.endsWith(".local") ||
    normalized === "0.0.0.0" ||
    normalized === "::1"
  ) {
    return true;
  }
  const match = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/u.exec(normalized);
  if (!match) return false;
  const octets = match.slice(1).map(Number);
  return (
    octets.some((part) => part > 255) ||
    octets[0] === 10 ||
    octets[0] === 127 ||
    octets[0] === 0 ||
    (octets[0] === 169 && octets[1] === 254) ||
    (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
    (octets[0] === 192 && octets[1] === 168)
  );
}

function safeHttpsUrl(value: unknown) {
  if (typeof value !== "string" || value.length > 2_048) return null;
  try {
    const url = new URL(value);
    if (
      url.protocol !== "https:" ||
      url.username ||
      url.password ||
      isUnsafeHostname(url.hostname)
    ) {
      return null;
    }
    return url;
  } catch {
    return null;
  }
}

function decodeDataRegistration(uri: string) {
  const base64Prefix = "data:application/json;base64,";
  const jsonPrefix = "data:application/json,";
  try {
    if (uri.startsWith(base64Prefix)) {
      const binary = atob(uri.slice(base64Prefix.length));
      if (binary.length > MAX_REGISTRATION_BYTES) return null;
      const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
      return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    }
    if (uri.startsWith(jsonPrefix)) {
      const decoded = decodeURIComponent(uri.slice(jsonPrefix.length));
      if (decoded.length > MAX_REGISTRATION_BYTES) return null;
      return JSON.parse(decoded);
    }
  } catch {
    return null;
  }
  return null;
}

function registrationFetchUrl(agentUri: string) {
  if (agentUri.startsWith("ipfs://")) {
    const path = agentUri.slice("ipfs://".length).replace(/^ipfs\//u, "");
    if (!/^[A-Za-z0-9][A-Za-z0-9._~!$&'()*+,;=:@/-]{1,2047}$/u.test(path)) {
      return null;
    }
    return new URL(`https://ipfs.io/ipfs/${path}`);
  }
  return safeHttpsUrl(agentUri);
}

async function fetchRegistration(url: URL) {
  const response = await fetch(url, {
    headers: { accept: "application/json" },
    // Cloudflare's edge fetch supports manual redirect handling but not
    // redirect:"error". Manual preserves the fail-closed no-redirect policy.
    redirect: "manual",
    signal: AbortSignal.timeout(REGISTRATION_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error(`registration returned HTTP ${response.status}`);
  const contentLength = Number(response.headers.get("content-length") ?? "0");
  if (contentLength > MAX_REGISTRATION_BYTES) throw new Error("registration is too large");
  const text = await response.text();
  if (text.length > MAX_REGISTRATION_BYTES) throw new Error("registration is too large");
  return JSON.parse(text) as Registration;
}

function matchesRegistration(registration: Registration, agentId: string) {
  if (!Array.isArray(registration.registrations)) return false;
  return registration.registrations.some((entry) => {
    if (!entry || typeof entry !== "object") return false;
    const record = entry as Record<string, unknown>;
    return (
      String(record.agentId) === agentId &&
      typeof record.agentRegistry === "string" &&
      record.agentRegistry.toLowerCase() === AGENT_REGISTRY.toLowerCase()
    );
  });
}

function selectService(registration: Registration): Service | null {
  if (!Array.isArray(registration.services)) return null;
  const valid = registration.services.flatMap((entry) => {
    if (!entry || typeof entry !== "object") return [];
    const record = entry as Record<string, unknown>;
    const endpoint = safeHttpsUrl(record.endpoint);
    if (typeof record.name !== "string" || !endpoint) return [];
    return [{
      name: record.name,
      endpoint: endpoint.toString(),
      ...(typeof record.version === "string" ? { version: record.version } : {}),
    }];
  });
  valid.sort((left, right) => {
    const leftIndex = SERVICE_PRIORITY.indexOf(left.name.toLowerCase());
    const rightIndex = SERVICE_PRIORITY.indexOf(right.name.toLowerCase());
    return (leftIndex < 0 ? 99 : leftIndex) - (rightIndex < 0 ? 99 : rightIndex);
  });
  return valid[0] ?? null;
}

export async function resolveErc8004Agent(
  rawAgentId: string | null,
): Promise<Erc8004ResolutionResult> {
  const agentId = parseAgentId(rawAgentId);
  if (!agentId) {
    return failure(422, "invalid_agent_id", "agent_id must be a base-10 uint256.");
  }

  let ownerResult: string;
  let uriResult: string;
  let walletResult: string;
  let rpc: string;
  try {
    const calls = await ethCalls([
      `${OWNER_OF_SELECTOR}${agentId.hex}`,
      `${TOKEN_URI_SELECTOR}${agentId.hex}`,
      `${GET_AGENT_WALLET_SELECTOR}${agentId.hex}`,
    ]);
    rpc = calls.rpc;
    [ownerResult, uriResult, walletResult] = calls.results;
  } catch (error) {
    return failure(
      502,
      "registry_unavailable",
      error instanceof Error ? error.message : "Base registry was unreachable.",
    );
  }

  const owner = parseAddress(ownerResult);
  const agentWallet = parseAddress(walletResult);
  const agentUri = decodeAbiString(uriResult);
  if (!owner || !agentWallet || !agentUri) {
    return failure(404, "agent_not_found", "The ERC-8004 identity is not registered with a usable agentURI.");
  }

  let registration: Registration;
  let registrationUrl: URL | null = null;
  const inline = decodeDataRegistration(agentUri);
  try {
    if (inline && typeof inline === "object") {
      registration = inline as Registration;
    } else {
      registrationUrl = registrationFetchUrl(agentUri);
      if (!registrationUrl) {
        return failure(422, "unsupported_agent_uri", "agentURI must use HTTPS, IPFS, or a JSON data URI.");
      }
      registration = await fetchRegistration(registrationUrl);
    }
  } catch (error) {
    return failure(
      502,
      "registration_unavailable",
      error instanceof Error ? error.message : "The registration file was unreachable.",
    );
  }

  if (!matchesRegistration(registration, agentId.decimal)) {
    return failure(409, "identity_mismatch", "The registration file does not bind this Base registry and agent_id.");
  }
  if (registration.active === false) {
    return failure(409, "agent_inactive", "The ERC-8004 registration marks this agent inactive.");
  }
  const service = selectService(registration);
  if (!service) {
    return failure(422, "no_https_service", "The registration advertises no public HTTPS service to preflight.");
  }

  const serviceUrl = new URL(service.endpoint);
  let domainVerification: Erc8004Resolution["endpoint_domain_verification"];
  if (registrationUrl && registrationUrl.origin === serviceUrl.origin) {
    domainVerification = {
      status: "registration-origin",
      verification_url: registrationUrl.toString(),
    };
  } else {
    const verificationUrl = new URL("/.well-known/agent-registration.json", serviceUrl.origin);
    let verification: Registration;
    try {
      verification = await fetchRegistration(verificationUrl);
    } catch (error) {
      return failure(
        409,
        "endpoint_domain_unverified",
        error instanceof Error ? error.message : "Endpoint-domain verification was unavailable.",
      );
    }
    if (!matchesRegistration(verification, agentId.decimal)) {
      return failure(409, "endpoint_domain_unverified", "The endpoint domain does not publish the matching ERC-8004 registration.");
    }
    domainVerification = {
      status: "well-known",
      verification_url: verificationUrl.toString(),
    };
  }

  return {
    ok: true,
    value: {
      schema: "agent-guild/erc8004-resolution/v1",
      chain: { namespace: "eip155", chain_id: BASE_CHAIN_ID, rpc },
      identity_registry: IDENTITY_REGISTRY,
      agent_registry: AGENT_REGISTRY,
      agent_id: agentId.decimal,
      owner,
      agent_wallet:
        agentWallet === "0x0000000000000000000000000000000000000000"
          ? null
          : agentWallet,
      agent_uri: agentUri,
      registration: {
        type: typeof registration.type === "string" ? registration.type : null,
        name: typeof registration.name === "string" ? registration.name : null,
        description:
          typeof registration.description === "string" ? registration.description : null,
        active: typeof registration.active === "boolean" ? registration.active : null,
        x402_support:
          typeof registration.x402Support === "boolean" ? registration.x402Support : null,
        supported_trust: Array.isArray(registration.supportedTrust)
          ? registration.supportedTrust.filter((value): value is string => typeof value === "string")
          : [],
        registration_matches_onchain_identity: true,
      },
      service,
      endpoint_domain_verification: domainVerification,
      resolved_at: new Date().toISOString(),
    },
  };
}

export function erc8004CorsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers":
      "Accept, PAYMENT-SIGNATURE, X-PAYMENT",
    "Access-Control-Expose-Headers":
      "PAYMENT-REQUIRED, PAYMENT-RESPONSE, X-PAYMENT-RESPONSE, X-Agent-Guild-Canonical-Resource, X-ERC8004-Agent-Registry, X-ERC8004-Agent-Id",
    "Cache-Control": "no-store",
  };
}

export async function signedErc8004Preflight(
  resolution: Erc8004Resolution,
  agentGuildResult: unknown,
  paymentResponse: string | null,
  origin: string,
  privateKeyPkcs8Base64?: string,
  publicJwkJson?: string,
) {
  const issuedAt = new Date();
  const artifact = {
    type: "agent-guild/erc8004-preflight/v1",
    issuer: {
      id: origin,
      boundary:
        "Worker-signed binding of on-chain ERC-8004 identity evidence to a canonical Agent Guild deep-preflight result; not an ERC-8004 Validation Registry claim.",
    },
    subject: resolution,
    agent_guild: {
      canonical_preflight: `${GUILD_BASE}/preflight/deep?url=${encodeURIComponent(resolution.service.endpoint)}`,
      result: agentGuildResult,
      payment_response: paymentResponse,
      settlement_boundary:
        "Payment terminates at Agent Guild. This worker takes no custody and stores no payment material.",
    },
    issued_at: issuedAt.toISOString(),
    expires_at: new Date(issuedAt.getTime() + 5 * 60_000).toISOString(),
    verification: {
      worker_key: `${origin}/.well-known/worker-signing-key.json`,
      registry_source: resolution.chain.rpc,
      standard: "https://eips.ethereum.org/EIPS/eip-8004",
    },
  };
  const signed = await signAgentCard(
    artifact,
    origin,
    privateKeyPkcs8Base64,
    publicJwkJson,
  );
  return "signatures" in signed ? signed : null;
}
