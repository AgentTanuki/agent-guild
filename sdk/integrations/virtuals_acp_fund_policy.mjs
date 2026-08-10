// Fail-closed Agent Guild counterparty gate for @virtuals-protocol/acp-node-v2.
//
// Free identity resolution binds the exact provider wallet + chain to a DID.
// The configured metered fetch then obtains the live risk decision (it may be
// an @x402/fetch wrapper or a fetch using a funded Agent Guild API key).

import {
  DEFAULT_HOST,
  verifyCredential,
  verifyJcsDocument,
} from "../agentguild_verify.mjs";

const NETWORK_BY_CHAIN = new Map([
  [8453, "eip155:8453"],
  [84532, "eip155:84532"],
]);

function normalizeHost(host) {
  return String(host || DEFAULT_HOST).replace(/\/$/, "");
}

function normalizeAddress(address, label = "providerAddress") {
  const out = String(address || "").toLowerCase();
  if (!/^0x[0-9a-f]{40}$/.test(out)) {
    throw new Error(`${label} is not a valid EVM address`);
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

function resourceFor(option, context) {
  const value = typeof option === "function" ? option(context) : option;
  const out = String(value || "");
  if (!/^https?:\/\//.test(out)) {
    throw new Error("resource must resolve to an exact http(s) job URL");
  }
  return out;
}

function samePayment(actual, expected) {
  return actual?.scheme === expected.scheme
    && actual?.network === expected.network
    && String(actual?.asset || "").toLowerCase() === expected.asset
    && actual?.amount === expected.amount
    && String(actual?.pay_to || "").toLowerCase() === expected.pay_to
    && actual?.resource === expected.resource;
}

/**
 * Create an ACP `fundPolicy` backed by one paid, signed AGPD-1 decision.
 *
 * The decision binds the exact provider wallet, CAIP-2 chain, token contract,
 * atomic amount and caller-supplied job URL immediately before `fund()` is
 * allowed to proceed. `meteredFetch` must use a separate x402 payment client;
 * the ACP policy never signs or pays for its own decision recursively.
 */
export function createAgentGuildAcpPaymentPolicy({
  host = DEFAULT_HOST,
  fetchImpl = globalThis.fetch,
  meteredFetch = null,
  apiKey = null,
  resource = null,
  capability = null,
  maxRisk = 32.99,
  minConfidence = 0.5,
  ttlSeconds = 300,
  maxAmountAtomic = null,
  pinIssuer = true,
  now = () => new Date(),
  onDecision = null,
} = {}) {
  if (typeof fetchImpl !== "function") {
    throw new TypeError("fetchImpl must be a function");
  }
  const decisionFetch = meteredFetch || (apiKey ? fetchImpl : null);
  if (typeof decisionFetch !== "function") {
    throw new TypeError(
      "meteredFetch is required unless a funded Agent Guild apiKey is supplied"
    );
  }
  if (meteredFetch === fetchImpl && !apiKey) {
    throw new TypeError(
      "meteredFetch must be a separate unguarded x402 transport to avoid recursion"
    );
  }
  const base = normalizeHost(host);

  return async function agentGuildAcpPaymentPolicy(context) {
    try {
      const network = NETWORK_BY_CHAIN.get(Number(context?.chainId));
      if (!network) {
        return { allow: false, reason: `unsupported settlement chain ${context?.chainId}` };
      }
      const amount = String(context?.amount?.rawAmount ?? "");
      if (!/^[0-9]+$/.test(amount) || BigInt(amount) <= 0n) {
        throw new Error("amount.rawAmount is not a positive atomic-unit integer");
      }
      if (maxAmountAtomic !== null && BigInt(amount) > BigInt(maxAmountAtomic)) {
        return {
          allow: false,
          reason: "funding amount exceeds the caller's local maxAmountAtomic policy",
        };
      }
      const expected = {
        scheme: "exact",
        network,
        asset: normalizeAddress(context?.amount?.address, "amount.address"),
        amount,
        pay_to: normalizeAddress(context?.providerAddress),
        resource: resourceFor(resource, context),
      };
      const requestedCapability = capabilityFor(capability, context);
      const body = {
        payment: expected,
        capability: requestedCapability,
        policy: {
          max_risk: Number(maxRisk),
          min_confidence: Number(minConfidence),
        },
        ttl_seconds: Number(ttlSeconds),
      };
      const headers = {
        accept: "application/json",
        "content-type": "application/json",
      };
      if (apiKey) headers["X-API-Key"] = apiKey;
      const decision = await getJson(
        decisionFetch,
        `${base}/wallet-binding/decision`,
        { method: "POST", headers, body: JSON.stringify(body) },
        "Agent Guild signed ACP funding decision"
      );
      const expectedIssuer = pinIssuer
        ? (await getJson(
            fetchImpl,
            `${base}/.well-known/agent-guild-did.json`,
            { headers: { accept: "application/json" } },
            "issuer DID discovery"
          )).did
        : null;
      const subject = decision?.credentialSubject || {};
      const validFrom = new Date(decision?.validFrom);
      const validUntil = new Date(decision?.validUntil);
      const clock = now();
      const fresh = Number.isFinite(validFrom.getTime())
        && Number.isFinite(validUntil.getTime())
        && validFrom <= clock && clock <= validUntil;
      const proofValid = verifyCredential(decision)
        && (!expectedIssuer || decision.issuer === expectedIssuer);
      const exact = samePayment(subject.payment, expected);
      const effective = subject?.policy?.effective || {};
      const policyExact = Number(effective.max_risk) <= Number(maxRisk)
        && Number(effective.min_confidence) >= Number(minConfidence);
      const capabilityExact = requestedCapability
        ? subject?.counterparty?.agent?.capabilities?.includes(requestedCapability)
        : true;
      const permitted = proofValid && fresh && exact && policyExact
        && capabilityExact && subject.contract === "AGPD-1/1.0"
        && subject.decision === "allow";
      if (typeof onDecision === "function") await onDecision(decision, context);
      const sealedBlock = proofValid && fresh && exact && policyExact
        && capabilityExact && subject.contract === "AGPD-1/1.0"
        && subject.decision !== "allow";
      return {
        allow: permitted,
        reason: permitted
          ? "exact ACP funding terms have a valid signed Agent Guild allow decision"
          : sealedBlock && subject.reason
            ? subject.reason
            : "signed ACP funding decision was invalid, stale, inexact, or did not allow payment",
        evidence: { decision },
      };
    } catch (error) {
      return {
        allow: false,
        reason: `counterparty payment verification unavailable: ${error?.message || error}`,
      };
    }
  };
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
