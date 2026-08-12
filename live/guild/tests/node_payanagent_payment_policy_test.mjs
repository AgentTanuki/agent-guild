import assert from "node:assert/strict";

import { createPaymentPolicy } from "../../../sdk/integrations/payanagent_payment_policy.mjs";

const signer = {
  address: "0x" + "88".repeat(20),
  async signMessage() { return "0x" + "11".repeat(65); },
};

function context(overrides = {}) {
  return {
    signer,
    walletAddress: signer.address,
    fetchImpl: async () => new Response("{}", { status: 500 }),
    createUnguardedPaidFetch: () => async () =>
      new Response("{}", { status: 402 }),
    payanAgentBaseUrl: "https://payanagent.example",
    ...overrides,
  };
}

const protectedHook = createPaymentPolicy(context());
assert.equal(typeof protectedHook, "function");
const blockedUnpaid = await protectedHook({
  paymentRequired: { resource: { url: "https://payanagent.com/x402/test" } },
  selectedRequirements: {
    scheme: "exact",
    network: "eip155:8453",
    asset: "0x" + "77".repeat(20),
    amount: "25000",
    payTo: "0x" + "66".repeat(20),
  },
});
assert.equal(blockedUnpaid.abort, true);
assert.match(blockedUnpaid.reason, /requires payment/);

let policyCalls = 0;
const cappedHook = createPaymentPolicy(context({
  createUnguardedPaidFetch: () => async () => {
    policyCalls += 1;
    return new Response("{}", { status: 500 });
  },
}), { maxAmountAtomic: "24999" });
const blockedCap = await cappedHook({
  paymentRequired: { resource: { url: "https://payanagent.com/x402/test" } },
  selectedRequirements: {
    scheme: "exact",
    network: "eip155:8453",
    asset: "0x" + "77".repeat(20),
    amount: "25000",
    payTo: "0x" + "66".repeat(20),
  },
});
assert.equal(blockedCap.abort, true);
assert.match(blockedCap.reason, /maxAmountAtomic/);
assert.equal(policyCalls, 0);

assert.equal(typeof createPaymentPolicy(context(), { mode: "standard" }), "function");
assert.throws(() => createPaymentPolicy(context(), { mode: "anything" }),
  /must be protected or standard/);
assert.throws(() => createPaymentPolicy(context(), { maxRisk: 101 }),
  /between 0 and 100/);
assert.throws(() => createPaymentPolicy({ ...context(), signer: null }),
  /must expose an EVM signer/);
assert.throws(() => createPaymentPolicy({
  ...context(), createUnguardedPaidFetch: null,
}), /must expose createUnguardedPaidFetch/);

console.log("payanagent payment policy: protected default, unpaid, cap, standard, validation ok");
