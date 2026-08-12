// Source entry for the single-file PayanAgent MCP payment policy.
//
// Build the public file with:
//   esbuild sdk/integrations/payanagent_payment_policy.entry.mjs \
//     --bundle --platform=node --format=esm \
//     --external:@x402/fetch --external:@x402/evm/exact/client \
//     --outfile=sdk/integrations/payanagent_payment_policy.mjs

import { createAgentGuildX402PaymentPolicy } from "./x402_payment_policy.mjs";

const DEFAULT_MODE = "protected";

function envValue(name) {
  return globalThis.process?.env?.[name];
}

function optionalInteger(value, label) {
  if (value === undefined || value === null || value === "") return null;
  const out = String(value);
  if (!/^[0-9]+$/.test(out)) {
    throw new TypeError(`${label} must be an unsigned integer string`);
  }
  return out;
}

function finiteNumber(value, fallback, label) {
  const out = value === undefined || value === null || value === ""
    ? Number(fallback)
    : Number(value);
  if (!Number.isFinite(out)) throw new TypeError(`${label} must be finite`);
  return out;
}

function positiveInteger(value, fallback, label) {
  const out = finiteNumber(value, fallback, label);
  if (!Number.isSafeInteger(out) || out <= 0) {
    throw new TypeError(`${label} must be a positive safe integer`);
  }
  return out;
}

/**
 * PayanAgent MCP `PAYANAGENT_PAYMENT_POLICY_MODULE` factory.
 *
 * The default `protected` mode buys and locally verifies one value-priced,
 * short-lived AGPD-1 credential before each Payan x402 payment is signed. The
 * same Base EOA signs the caller proof and later funds the Payan purchase.
 * `standard` mode uses the flat-price exact-payment decision instead.
 */
export function createPaymentPolicy(context, options = {}) {
  if (!context?.signer || typeof context.signer.address !== "string"
      || typeof context.signer.signMessage !== "function") {
    throw new TypeError("PayanAgent policy context must expose an EVM signer");
  }
  if (typeof context.createUnguardedPaidFetch !== "function") {
    throw new TypeError(
      "PayanAgent policy context must expose createUnguardedPaidFetch()",
    );
  }
  const mode = String(
    options.mode ?? envValue("AGENT_GUILD_PAYAN_POLICY_MODE") ?? DEFAULT_MODE,
  ).toLowerCase();
  if (!new Set(["protected", "standard"]).has(mode)) {
    throw new TypeError(
      "AGENT_GUILD_PAYAN_POLICY_MODE must be protected or standard",
    );
  }
  const maxAmountAtomic = optionalInteger(
    options.maxAmountAtomic
      ?? envValue("AGENT_GUILD_PAYAN_MAX_AMOUNT_ATOMIC"),
    "AGENT_GUILD_PAYAN_MAX_AMOUNT_ATOMIC",
  );
  const maxDecisionFeeCredits = positiveInteger(
    options.maxDecisionFeeCredits
      ?? envValue("AGENT_GUILD_PAYAN_MAX_DECISION_FEE_CREDITS"),
    10_000_000,
    "AGENT_GUILD_PAYAN_MAX_DECISION_FEE_CREDITS",
  );
  const maxRisk = finiteNumber(
    options.maxRisk ?? envValue("AGENT_GUILD_PAYAN_MAX_RISK"),
    32.99,
    "AGENT_GUILD_PAYAN_MAX_RISK",
  );
  const minConfidence = finiteNumber(
    options.minConfidence ?? envValue("AGENT_GUILD_PAYAN_MIN_CONFIDENCE"),
    0.5,
    "AGENT_GUILD_PAYAN_MIN_CONFIDENCE",
  );
  if (maxRisk < 0 || maxRisk > 100) {
    throw new RangeError("AGENT_GUILD_PAYAN_MAX_RISK must be between 0 and 100");
  }
  if (minConfidence < 0 || minConfidence > 1) {
    throw new RangeError(
      "AGENT_GUILD_PAYAN_MIN_CONFIDENCE must be between 0 and 1",
    );
  }

  return createAgentGuildX402PaymentPolicy({
    host: options.host,
    fetchImpl: options.fetchImpl ?? context.fetchImpl ?? globalThis.fetch,
    meteredFetch: context.createUnguardedPaidFetch(),
    maxAmountAtomic,
    maxDecisionFeeCredits,
    maxRisk,
    minConfidence,
    protectedValue: mode === "protected",
    evmSigner: mode === "protected" ? context.signer : null,
    onDecision: options.onDecision,
    now: options.now,
  });
}

export default createPaymentPolicy;
