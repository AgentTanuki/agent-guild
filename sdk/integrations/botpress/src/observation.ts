// Agent Guild observations by AgentTanuki, Apache-2.0.
// Adapted from the author's reviewed connector transport/projection; no native provider code copied.
const ORIGIN = "https://agent-guild-5d5r.onrender.com";
const MAX_BYTES = 65536;
const TIMEOUT_MS = 15000;

type CheckName = (typeof CHECKS)[number];
type CheckStatus = "proven" | "failed" | "unknown";
export type ObservationOutput = {
  status: "observed" | "rejected" | "unavailable";
  code: string | null;
  serviceOrigin: string;
  target: string | null;
  requestedAt: string | null;
  completedAt: string | null;
  httpStatus: number | null;
  responseBytes: number | null;
  verdict:
    "do_not_delegate" | "delegate_with_caution" | "no_failed_checks" | null;
  checks: { check: CheckName; status: CheckStatus }[];
  failed: CheckName[];
  unknowns: CheckName[];
  scored: CheckName[];
  limitations: string[];
};

function parameters(inputs: Record<string, unknown>, keys: string[]) {
  const descriptors = Object.getOwnPropertyDescriptors(inputs);
  requireValue(
    Object.getOwnPropertySymbols(inputs).length === 0,
    "invalid_input",
  );
  requireValue(
    Object.keys(descriptors).length === keys.length &&
      keys.every(
        (key) => descriptors[key]?.enumerable && "value" in descriptors[key],
      ),
    "invalid_input",
  );
  return Object.fromEntries(keys.map((key) => [key, descriptors[key]!.value]));
}
class GuildError extends Error {
  constructor(public readonly code: string) {
    super(code);
  }
}

function requireValue(value: unknown, code: string): asserts value {
  if (!value) {
    throw new GuildError(code);
  }
}

function record(value: unknown): value is Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

// Lexical screening only: a valid DNS name may still resolve to a private address.
function endpoint(value: string): string {
  requireValue(
    value.length > 0 &&
      value.length <= 2048 &&
      !/[\s\\]/.test(value) &&
      [...value].every(
        (character) =>
          character.charCodeAt(0) >= 32 && character.charCodeAt(0) !== 127,
      ),
    "invalid_endpoint",
  );
  requireValue(
    new TextDecoder().decode(new TextEncoder().encode(value)) === value,
    "invalid_endpoint",
  );
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new GuildError("invalid_endpoint");
  }
  requireValue(
    ["http:", "https:"].includes(url.protocol) &&
      !url.username &&
      !url.password &&
      !url.hash,
    "invalid_endpoint",
  );
  // Restrict this operation to DNS hostnames; IP literals and legacy numeric IP forms are unsupported.
  const labels = url.hostname.split(".");
  requireValue(
    labels.length >= 2 &&
      labels.every((label) =>
        /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label),
      ) &&
      /^[a-z]{2,63}$/.test(labels[labels.length - 1] ?? "") &&
      ![
        "local",
        "localhost",
        "internal",
        "test",
        "invalid",
        "example",
        "onion",
        "home",
        "lan",
      ].includes(labels[labels.length - 1] ?? ""),
    "invalid_endpoint",
  );
  // The original string is retained and submitted; parsing is never used to rewrite it.
  return url.hostname;
}

// JSON.parse alone discards duplicate object keys. Reject ambiguity before projection.
function strictJson(text: string): unknown {
  let offset = 0;
  function whitespace() {
    while (/\s/.test(text[offset] ?? "") && offset < text.length) {
      offset++;
    }
  }
  function string(): string {
    const start = offset++;
    while (offset < text.length) {
      if (text[offset++] === '"') {
        return JSON.parse(text.slice(start, offset));
      }
      if (text[offset - 1] === "\\") {
        offset++;
      }
    }
    throw new GuildError("invalid_response");
  }
  function value(depth = 0): void {
    requireValue(depth <= 24, "invalid_response");
    whitespace();
    if (text[offset] === '"') {
      string();
      return;
    }
    if (text[offset] === "{") {
      offset++;
      whitespace();
      const keys = new Set<string>();
      if (text[offset] === "}") {
        offset++;
        return;
      }
      while (offset < text.length) {
        requireValue(text[offset] === '"', "invalid_response");
        const key = string();
        requireValue(!keys.has(key), "invalid_response");
        keys.add(key);
        whitespace();
        requireValue(text[offset++] === ":", "invalid_response");
        value(depth + 1);
        whitespace();
        if (text[offset] === "}") {
          offset++;
          return;
        }
        requireValue(text[offset++] === ",", "invalid_response");
        whitespace();
      }
    } else if (text[offset] === "[") {
      offset++;
      whitespace();
      if (text[offset] === "]") {
        offset++;
        return;
      }
      while (offset < text.length) {
        value(depth + 1);
        whitespace();
        if (text[offset] === "]") {
          offset++;
          return;
        }
        requireValue(text[offset++] === ",", "invalid_response");
      }
    } else {
      const match =
        /^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(
          text.slice(offset),
        );
      requireValue(match, "invalid_response");
      if (!["true", "false", "null"].includes(match[0])) {
        requireValue(Number.isFinite(Number(match[0])), "invalid_response");
      }
      offset += match[0].length;
      return;
    }
    throw new GuildError("invalid_response");
  }
  try {
    value();
    whitespace();
    requireValue(offset === text.length, "invalid_response");
    return JSON.parse(text);
  } catch {
    throw new GuildError("invalid_response");
  }
}

interface ResponseEvidence {
  value: unknown;
  httpStatus: number;
  responseBytes: number;
  requestedAt: string;
  completedAt: string;
}

async function requestJson(
  fetcher: typeof globalThis.fetch,
  path: string,
  timeoutMs: number,
  signal?: AbortSignal,
): Promise<ResponseEvidence> {
  const controller = new AbortController();
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  let timedOut = false;
  const requestedAt = new Date().toISOString();
  const serviceUrl = new URL(`${ORIGIN}${path}`).href;
  const cancel = () => controller.abort();
  const onAbort = () => {
    throw new GuildError(timedOut ? "request_timeout" : "request_aborted");
  };
  const aborted = new Promise<never>((_resolve, reject) => {
    controller.signal.addEventListener(
      "abort",
      () => {
        try {
          onAbort();
        } catch (error) {
          reject(error);
        }
      },
      { once: true },
    );
  });
  if (signal?.aborted) {
    controller.abort();
  } else {
    signal?.addEventListener("abort", cancel, { once: true });
  }
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    if (controller.signal.aborted) {
      return await aborted;
    }
    const work = async (): Promise<ResponseEvidence> => {
      const response = await fetcher(serviceUrl, {
        method: "GET",
        headers: {
          Accept: "application/json",
          "User-Agent": "AgentGuild-Botpress-EndpointObservations/0.1.0",
        },
        signal: controller.signal,
        redirect: "error",
        credentials: "omit",
        referrerPolicy: "no-referrer",
        mode: "cors",
      });
      requireValue(
        !response.redirected && response.status === 200,
        "http_unavailable",
      );
      requireValue(
        response.url === "" || response.url === serviceUrl,
        "unexpected_response_url",
      );
      requireValue(
        response.headers
          .get("content-type")
          ?.split(";")[0]
          ?.trim()
          .toLowerCase() === "application/json",
        "invalid_response",
      );
      requireValue(response.body, "invalid_response");
      reader = response.body.getReader();
      const chunks: Uint8Array[] = [];
      let size = 0;
      while (true) {
        const next = await reader.read();
        if (next.done) {
          break;
        }
        size += next.value.byteLength;
        requireValue(size <= MAX_BYTES, "response_too_large");
        chunks.push(next.value);
      }
      const bytes = new Uint8Array(size);
      let cursor = 0;
      for (const chunk of chunks) {
        bytes.set(chunk, cursor);
        cursor += chunk.byteLength;
      }
      const value = strictJson(
        new TextDecoder("utf-8", { fatal: true }).decode(bytes),
      );
      return {
        value,
        httpStatus: response.status,
        responseBytes: size,
        requestedAt,
        completedAt: new Date().toISOString(),
      };
    };
    return await Promise.race([work(), aborted]);
  } catch (error) {
    if (controller.signal.aborted) {
      throw new GuildError(timedOut ? "request_timeout" : "request_aborted");
    }
    throw error instanceof GuildError
      ? error
      : new GuildError("request_unavailable");
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
    // Cancelling a host-supplied stream must not extend the operation's deadline.
    if (reader) {
      void reader.cancel().catch(() => undefined);
    }
    controller.abort();
  }
}

export const CHECKS = [
  "endpoint_reachable",
  "protocol_handshake",
  "agent_card_resolves",
  "agent_card_signed",
  "payment_claim_holds",
  "independent_evidence",
] as const;

function observation(value: unknown, target: string) {
  requireValue(record(value) && value.target === target, "target_mismatch");
  requireValue(
    Array.isArray(value.checks) && value.checks.length === CHECKS.length,
    "incomplete_response",
  );
  const seen = new Map<CheckName, CheckStatus>();
  for (const item of value.checks) {
    requireValue(
      record(item) &&
        typeof item.check === "string" &&
        CHECKS.some((key) => key === item.check) &&
        !seen.has(item.check as CheckName),
      "invalid_response",
    );
    requireValue(
      typeof item.status === "string" &&
        ["proven", "failed", "unknown"].includes(item.status),
      "invalid_response",
    );
    seen.set(item.check as CheckName, item.status as CheckStatus);
  }
  const checks = CHECKS.map((check) => ({
    check,
    status: seen.get(check) as CheckStatus,
  }));
  const failed = checks
    .filter((check) => check.status === "failed")
    .map((check) => check.check);
  const unknowns = checks
    .filter((check) => check.status === "unknown")
    .map((check) => check.check);
  for (const [name, expected] of [
    ["failed", failed],
    ["unknowns", unknowns],
    [
      "scored",
      checks
        .filter((check) => check.status !== "unknown")
        .map((check) => check.check),
    ],
  ] as const) {
    const received = value[name];
    requireValue(
      Array.isArray(received) &&
        received.length === expected.length &&
        new Set(received).size === expected.length &&
        received.every((item) => expected.includes(item)),
      "inconsistent_response",
    );
  }
  const verdict: NonNullable<ObservationOutput["verdict"]> = failed.some(
    (check) => check === "endpoint_reachable" || check === "protocol_handshake",
  )
    ? "do_not_delegate"
    : failed.length
      ? "delegate_with_caution"
      : "no_failed_checks";
  requireValue(value.verdict === verdict, "inconsistent_response");
  return {
    target,
    verdict,
    checks,
    failed,
    unknowns,
    scored: checks
      .filter((item) => item.status !== "unknown")
      .map((item) => item.check),
    limitations: [
      "Service-reported observations of this exact URL; later execution is not bound to this result.",
      "Reachability and a protocol handshake do not establish task success or safe data handling.",
      "Card-signature presence is not cryptographic signature verification.",
      "A payment-claim observation is not proof of settlement. Independent ownership remains unverified.",
      "Unknown checks are excluded from the verdict and remain unknown. No permission to delegate or pay is granted.",
    ],
  };
}

export async function observeEndpoint(
  input: unknown,
): Promise<ObservationOutput> {
  try {
    requireValue(record(input), "invalid_input");
    const { url } = parameters(input, ["url"]);
    requireValue(typeof url === "string", "invalid_input");
    endpoint(url);
    const response = await requestJson(
      globalThis.fetch,
      `/preflight?url=${encodeURIComponent(url)}`,
      TIMEOUT_MS,
    );
    return {
      status: "observed",
      code: null,
      serviceOrigin: ORIGIN,
      requestedAt: response.requestedAt,
      completedAt: response.completedAt,
      httpStatus: response.httpStatus,
      responseBytes: response.responseBytes,
      ...observation(response.value, url),
    };
  } catch (error) {
    const code =
      error instanceof GuildError ? error.code : "operation_unavailable";
    return {
      status: ["invalid_input", "invalid_endpoint"].includes(code)
        ? "rejected"
        : "unavailable",
      code,
      serviceOrigin: ORIGIN,
      target: null,
      requestedAt: null,
      completedAt: null,
      httpStatus: null,
      responseBytes: null,
      verdict: null,
      checks: [],
      failed: [],
      unknowns: [],
      scored: [],
      limitations: [
        "No usable endpoint observation was returned. This result grants no permission to delegate, send data or pay.",
      ],
    };
  }
}
