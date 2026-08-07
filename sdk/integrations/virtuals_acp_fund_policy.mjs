// Fail-closed Agent Guild counterparty gate for @virtuals-protocol/acp-node-v2.
//
// Free identity resolution binds the exact provider wallet + chain to a DID.
// The configured metered fetch then obtains the live risk decision (it may be
// an @x402/fetch wrapper or a fetch using a funded Agent Guild API key).

import {
  DEFAULT_HOST,
  verifyJcsDocument,
} from "../agentguild_verify.mjs";

const NETWORK_BY_CHAIN = new Map([
  [8453, "eip155:8453"],
  [84532, "eip155:84532"],
]);

function normalizeHost(host) {
  return String(host || DEFAULT_HOST).replace(/\/$/, "");
}

function normalizeAddress(address) {
  const out = String(address || "").toLowerCase();
  if (!/^0x[0-9a-f]{40}$/.test(out)) {
    throw new Error("providerAddress is not a valid EVM address");
  }
  return out;
}

async function getJson(fetcher, url, init, label) {
  const response = await fetcher(url, init);
  if (response.status === 402) {
    throw new Error(
      `${label} requires payment; configure an x402-enabled meteredFetch `
      + "or a funded Agent Guild API key"
    );
  }
  if (!response.ok) throw new Error(`${label} failed: HTTP ${response.status}`);
  return response.json();
}

function capabilityFor(option, context) {
  if (typeof option === "function") return option(context);
  return option || null;
}

/**
 * Create an ACP `fundPolicy` that refuses to pay an unbound or unsafe wallet.
 *
 * `meteredFetch` should be an official x402-wrapped fetch for autonomous USDC
 * payment, or `apiKey` may identify a funded Agent Guild credit account.
 */
export function createAgentGuildFundPolicy({
  host = DEFAULT_HOST,
  fetchImpl = globalThis.fetch,
  meteredFetch = fetchImpl,
  apiKey = null,
  capability = null,
  allowedRecommendations = ["hire"],
  maxRisk = 50,
  minConfidence = 0.5,
  maxStatusAgeMs = 5 * 60 * 1000,
  pinIssuer = true,
  now = () => new Date(),
} = {}) {
  if (typeof fetchImpl !== "function" || typeof meteredFetch !== "function") {
    throw new TypeError("fetchImpl and meteredFetch must be functions");
  }
  const base = normalizeHost(host);

  return async function agentGuildFundPolicy(context) {
    try {
      const network = NETWORK_BY_CHAIN.get(Number(context.chainId));
      if (!network) {
        return { allow: false, reason: `unsupported settlement chain ${context.chainId}` };
      }
      const address = normalizeAddress(context.providerAddress);
      const resolutionUrl = new URL(`${base}/wallet-binding/resolve`);
      resolutionUrl.searchParams.set("address", address);
      resolutionUrl.searchParams.set("network", network);
      const resolution = await getJson(
        fetchImpl,
        resolutionUrl,
        { headers: { accept: "application/json" } },
        "wallet identity resolution"
      );

      if (resolution.status !== "bound_registered" || !resolution.agent) {
        return {
          allow: false,
          reason: "provider wallet has no active binding to a registered machine identity",
          evidence: { resolution },
        };
      }

      const credential = resolution.binding?.credential;
      const liveStatus = resolution.binding?.status;
      const expectedIssuer = pinIssuer
        ? (await getJson(
            fetchImpl,
            `${base}/.well-known/agent-guild-did.json`,
            { headers: { accept: "application/json" } },
            "issuer DID discovery"
          )).did
        : null;
      const signaturesValid = (
        verifyJcsDocument(credential, { expectedIssuer })
        && verifyJcsDocument(liveStatus, { expectedIssuer })
      );
      const asOf = new Date(liveStatus?.as_of);
      const expiresAt = new Date(credential?.expires_at);
      const statusFresh = (
        Number.isFinite(asOf.getTime())
        && Math.abs(now().getTime() - asOf.getTime()) <= maxStatusAgeMs
      );
      const exactBinding = (
        credential?.address === address
        && credential?.network === network
        && credential?.did === resolution.agent.did
        && liveStatus?.credential_id === credential?.credential_id
        && liveStatus?.status === "active"
        && Number.isFinite(expiresAt.getTime())
        && expiresAt > now()
      );
      if (!signaturesValid || !statusFresh || !exactBinding) {
        return {
          allow: false,
          reason: "wallet binding evidence is invalid, stale, expired, or not exact",
          evidence: { resolution, signaturesValid, statusFresh, exactBinding },
        };
      }

      const requiredCapability = capabilityFor(capability, context);
      if (requiredCapability
          && !resolution.agent.capabilities?.includes(requiredCapability)) {
        return {
          allow: false,
          reason: `bound agent does not advertise required capability: ${requiredCapability}`,
          evidence: { resolution },
        };
      }

      const riskUrl = `${base}/agents/${encodeURIComponent(resolution.agent.id)}/risk-score`;
      const headers = { accept: "application/json" };
      if (apiKey) headers["X-API-Key"] = apiKey;
      const risk = await getJson(
        meteredFetch,
        riskUrl,
        { headers },
        "Agent Guild risk decision"
      );
      const permitted = (
        allowedRecommendations.includes(risk.recommendation)
        && Number(risk.risk) <= maxRisk
        && Number(risk.confidence) >= minConfidence
      );
      return {
        allow: permitted,
        reason: permitted
          ? "exact payment wallet is bound to a registered agent that satisfies risk policy"
          : "bound agent does not satisfy the configured risk policy",
        evidence: {
          address,
          network,
          credential,
          liveStatus,
          agent: resolution.agent,
          risk,
        },
      };
    } catch (error) {
      return {
        allow: false,
        reason: `counterparty verification unavailable: ${error?.message || error}`,
      };
    }
  };
}
