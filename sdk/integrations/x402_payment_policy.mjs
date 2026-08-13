// Agent Guild pre-signature policy for the official x402 client.
//
// Register the returned hook with `client.onBeforePaymentCreation(...)`.  It
// buys one short-lived AgentGuildPaymentDecision bound to the selected exact
// payment requirements, verifies the Guild's eddsa-jcs-2022 proof and every
// payment field locally, and aborts before a payment payload is signed unless
// the sealed decision is `allow`.
//
// Optional AGSM-1 mode adds `mandateId` and `evmSigner`; it creates a unique
// authorization id by default, or accepts `mandateAuthorizationId(context)`.
// The same hook then seals
// and enforces cumulative, per-counterparty and authorization-count caps across
// processes and restarts before it allows the x402 client to sign.
//
// IMPORTANT: for ordinary paid AGPD-1, `meteredFetch` must use a
// separate, unguarded x402 client.  Reusing the protected client would invoke
// this hook recursively while trying to pay for its own decision.  A funded
// Agent Guild `apiKey` may instead use ordinary `fetchImpl`.

import {
  DEFAULT_HOST,
  verifyCredential,
} from "../agentguild_verify.mjs";
import {
  CALLER_PROOF_HEADER,
  createCallerProof,
  encodeCallerProof,
  evmWalletCallerProofSigner,
} from "../agentguild_envelope_client.mjs";
import { createHash, randomUUID } from "node:crypto";

function sha256Hex(value) {
  return createHash("sha256").update(String(value), "utf8").digest("hex");
}

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

export function expectedProtectedDecisionFeeCredits(amountAtomic, {
  feeBps = 25,
  minFeeCredits = 10,
  maxFeeCredits = 10_000_000,
} = {}) {
  const amount = BigInt(amountAtomic);
  const rawAtomic = (amount * BigInt(feeBps) + 9_999n) / 10_000n;
  const rawCredits = (rawAtomic + 999n) / 1_000n;
  return Number(
    rawCredits < BigInt(minFeeCredits)
      ? BigInt(minFeeCredits)
      : rawCredits > BigInt(maxFeeCredits)
        ? BigInt(maxFeeCredits)
        : rawCredits
  );
}

async function buyDecision({
  base, endpointPath, decisionFetch, apiKey, body, callerSigner,
}) {
  const rawBody = JSON.stringify(body);
  const headers = {
    accept: "application/json",
    "content-type": "application/json",
    "user-agent": "agent-guild-x402-payment-policy/2.3.0",
  };
  if (apiKey) headers["X-API-Key"] = apiKey;
  if (callerSigner) {
    const proof = await createCallerProof({
      signer: callerSigner,
      method: "POST",
      resource: endpointPath,
      body: rawBody,
    });
    headers[CALLER_PROOF_HEADER] = encodeCallerProof(proof);
  }
  return getJson(
    decisionFetch,
    `${base}${endpointPath}`,
    { method: "POST", headers, body: rawBody },
    "Agent Guild signed payment decision"
  );
}

/**
 * Create an official x402 `onBeforePaymentCreation` hook.
 *
 * `meteredFetch` is deliberately required for ordinary paid AGPD-1 unless an
 * `apiKey` is supplied. AGSM-1 mandate decisions are free and use `fetchImpl`
 * directly, so they cannot recurse through an x402 settlement.
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
  protectedValue = false,
  evmSigner = null,
  maxDecisionFeeCredits = 10_000_000,
  mandateId = null,
  mandateAuthorizationId = null,
} = {}) {
  if (typeof fetchImpl !== "function") {
    throw new TypeError("fetchImpl must be a function");
  }
  const mandateConfigured = mandateId !== null;
  if (!mandateConfigured && mandateAuthorizationId !== null) {
    throw new TypeError(
      "mandateAuthorizationId cannot be configured without mandateId"
    );
  }
  if (mandateAuthorizationId !== null
      && typeof mandateAuthorizationId !== "function") {
    throw new TypeError(
      "mandateAuthorizationId must be a per-invocation function or omitted"
    );
  }
  const paidDecisionFetch = meteredFetch || (apiKey ? fetchImpl : null);
  if (!mandateConfigured && typeof paidDecisionFetch !== "function") {
    throw new TypeError(
      "meteredFetch is required unless a funded Agent Guild apiKey is supplied"
    );
  }
  if (meteredFetch === fetchImpl && !apiKey && !mandateConfigured) {
    throw new TypeError(
      "meteredFetch must be a separate unguarded x402 transport to avoid recursion"
    );
  }
  const base = normalizeHost(host);
  if (protectedValue && !evmSigner) {
    throw new TypeError("evmSigner is required for protectedValue payer continuity");
  }
  if (protectedValue && typeof meteredFetch !== "function") {
    throw new TypeError("protectedValue requires meteredFetch; API keys cannot buy this mainnet-only product");
  }
  if (mandateConfigured && !evmSigner) {
    throw new TypeError("evmSigner is required for AGSM-1 spend mandates");
  }
  if (mandateConfigured && protectedValue) {
    throw new TypeError(
      "AGSM-1 mandates and protectedValue cannot be combined in version 1"
    );
  }
  const callerSigner = (protectedValue || mandateConfigured)
    ? evmWalletCallerProofSigner(evmSigner)
    : null;
  const usedAuthorizationIds = new Set();
  const usedAuthorizationIdOrder = [];

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

      if (mandateConfigured) {
        const selectedMandateId = resolveOption(mandateId, context);
        const selectedAuthorizationId = mandateAuthorizationId === null
          ? `agsm-auth-${randomUUID()}`
          : resolveOption(mandateAuthorizationId, context);
        if (!selectedMandateId || !selectedAuthorizationId) {
          throw new Error("mandate configuration resolved to an empty value");
        }
        const authorizationKey = String(selectedAuthorizationId);
        if (usedAuthorizationIds.has(authorizationKey)) {
          throw new Error(
            "mandateAuthorizationId was reused; every payment attempt needs a unique id"
          );
        }
        usedAuthorizationIds.add(authorizationKey);
        usedAuthorizationIdOrder.push(authorizationKey);
        if (usedAuthorizationIdOrder.length > 2048) {
          usedAuthorizationIds.delete(usedAuthorizationIdOrder.shift());
        }
        const authorization = await buyDecision({
          base,
          endpointPath: "/mandates/authorize",
          decisionFetch: fetchImpl,
          apiKey: null,
          body: {
            mandate_id: String(selectedMandateId),
            authorization_id: String(selectedAuthorizationId),
            payment: expected,
          },
          callerSigner,
        });
        const expectedIssuer = pinIssuer
          ? (await getJson(
              fetchImpl,
              `${base}/.well-known/agent-guild-did.json`,
              { headers: { accept: "application/json" } },
              "issuer DID discovery"
            )).did
          : null;
        const subject = authorization?.credentialSubject || {};
        const validFrom = new Date(authorization?.validFrom);
        const validUntil = new Date(authorization?.validUntil);
        const clock = now();
        const fresh = Number.isFinite(validFrom.getTime())
          && Number.isFinite(validUntil.getTime())
          && validFrom <= clock && clock <= validUntil;
        const proofValid = verifyCredential(authorization)
          && (!expectedIssuer || authorization.issuer === expectedIssuer);
        const permitted = proofValid && fresh
          && subject.contract === "AGSM-1/1.0"
          && subject.mandate_id === String(selectedMandateId)
          && subject.authorization_id_sha256 === sha256Hex(
            String(selectedAuthorizationId))
          && subject.id === callerSigner.did
          && samePayment(subject.payment, expected)
          && subject.authorized === true
          && subject.decision === "allow"
          && subject.idempotent_replay === false;
        if (typeof onDecision === "function") {
          await onDecision(authorization, context);
        }
        if (!permitted) {
          return {
            abort: true,
            reason: subject.decision === "block" && subject.failures?.length
              ? subject.failures.join("; ")
              : "signed spend authorization was invalid, stale, inexact, replayed, or blocked",
          };
        }
        // Budget-only mode is useful by itself.  If an existing paid AGPD-1
        // transport/key is also configured, compose both gates: authority AND
        // counterparty trust must allow before the client signs.
        if (typeof paidDecisionFetch !== "function") return undefined;
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
      if (protectedValue) {
        const expectedFee = expectedProtectedDecisionFeeCredits(expected.amount);
        if (expectedFee > Number(maxDecisionFeeCredits)) {
          return {
            abort: true,
            reason: "protected-value decision fee exceeds maxDecisionFeeCredits",
          };
        }
      }
      const endpointPath = protectedValue
        ? "/wallet-binding/protected-decision"
        : "/wallet-binding/decision";
      const decision = await buyDecision({
        base, endpointPath, decisionFetch: paidDecisionFetch,
        apiKey, body, callerSigner,
      });
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
      const expectedFeeCredits = protectedValue
        ? expectedProtectedDecisionFeeCredits(expected.amount)
        : null;
      const protection = subject?.protection || {};
      const protectionExact = !protectedValue || (
        protection.contract === "agent-guild/protected-value-policy/v1"
        && protection?.pricing?.contract === "agent-guild/protected-value-pricing/v1"
        && protection?.pricing?.basis_points === 25
        && protection?.pricing?.minimum_fee_credits === 10
        && protection?.pricing?.maximum_fee_credits === 10_000_000
        && protection?.pricing?.fee_credits === expectedFeeCredits
        && protection?.pricing?.protected_amount_atomic === expected.amount
        && protection?.service_client?.caller_did === callerSigner.did
        && protection?.service_client?.payer_eoa === evmSigner.address.toLowerCase()
        && protection?.reachability?.recommended_for_routing === true
        && protection?.value_at_risk?.tiers?.[protection.required_value_tier] === true
      );
      const permitted = proofValid && fresh && exact && policyExact
        && capabilityExact && subject.contract === "AGPD-1/1.0"
        && protectionExact
        && subject.decision === "allow";
      if (typeof onDecision === "function") await onDecision(decision, context);
      if (!permitted) {
        const sealedBlock = proofValid && fresh && exact && policyExact
          && capabilityExact && subject.contract === "AGPD-1/1.0"
          && protectionExact
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
