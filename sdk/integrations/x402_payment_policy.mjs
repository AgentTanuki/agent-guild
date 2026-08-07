// Agent Guild pre-signature policy for the official x402 client.
//
// Register the returned hook with `client.onBeforePaymentCreation(...)`.  It
// buys one short-lived AgentGuildPaymentDecision bound to the selected exact
// payment requirements, verifies the Guild's eddsa-jcs-2022 proof and every
// payment field locally, and aborts before a payment payload is signed unless
// the sealed decision is `allow`.
//
// IMPORTANT: when paying Agent Guild with x402, `meteredFetch` must use a
// separate, unguarded x402 client.  Reusing the protected client would invoke
// this hook recursively while trying to pay for its own decision.  A funded
// Agent Guild `apiKey` may instead use ordinary `fetchImpl`.

import {
  DEFAULT_HOST,
  verifyCredential,
} from "../agentguild_verify.mjs";

function normalizeHost(host) {
  return String(host || DEFAULT_HOST).replace(/\/$/, "");
}

function normalizeAddress(value, label) {
  const out = String(value || "").toLowerCase();
  if (!/^0x[0-9a-f]{40}$/.test(out)) {
    throw new Error(`${label} is not an exact EVM address`);
  }
  return out;
}

async function getJson(fetcher, url, init, label) {
  const response = await fetcher(url, init);
  if (response.status === 402) {
    throw new Error(
      `${label} requires payment; configure a separate unguarded x402 `
      + "meteredFetch or a funded Agent Guild apiKey"
    );
  }
  if (!response.ok) throw new Error(`${label} failed: HTTP ${response.status}`);
  return response.json();
}

function resolveOption(option, context) {
  return typeof option === "function" ? option(context) : option;
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
 * Create an official x402 `onBeforePaymentCreation` hook.
 *
 * `meteredFetch` is deliberately required unless `apiKey` is supplied.  This
 * prevents accidental recursion through the same guarded x402 client.
 */
export function createAgentGuildX402PaymentPolicy({
  host = DEFAULT_HOST,
  fetchImpl = globalThis.fetch,
  meteredFetch = null,
  apiKey = null,
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

  return async function agentGuildX402PaymentPolicy(context) {
    try {
      const selected = context?.selectedRequirements || {};
      const resource = String(context?.paymentRequired?.resource?.url || "");
      const expected = {
        scheme: String(selected.scheme || ""),
        network: String(selected.network || ""),
        asset: normalizeAddress(selected.asset, "asset"),
        amount: String(selected.amount || ""),
        pay_to: normalizeAddress(selected.payTo, "payTo"),
        resource,
      };
      if (!/^[0-9]+$/.test(expected.amount) || BigInt(expected.amount) <= 0n) {
        throw new Error("amount is not a positive atomic-unit integer string");
      }
      if (!/^https?:\/\//.test(expected.resource)) {
        throw new Error("payment resource is not an http(s) URL");
      }
      if (maxAmountAtomic !== null
          && BigInt(expected.amount) > BigInt(maxAmountAtomic)) {
        return {
          abort: true,
          reason: "payment exceeds the caller's local maxAmountAtomic policy",
        };
      }

      const requestedCapability = resolveOption(capability, context) || null;
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
        "Agent Guild signed payment decision"
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
      if (!permitted) {
        const sealedBlock = proofValid && fresh && exact && policyExact
          && capabilityExact && subject.contract === "AGPD-1/1.0"
          && subject.decision !== "allow";
        return {
          abort: true,
          reason: sealedBlock && subject.reason
            ? subject.reason
            : "signed payment decision was invalid, stale, inexact, or did not allow payment",
        };
      }
      // Returning void is the official x402 hook's allow contract.
      return undefined;
    } catch (error) {
      return {
        abort: true,
        reason: `counterparty payment verification unavailable: ${error?.message || error}`,
      };
    }
  };
}
