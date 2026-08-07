import assert from "node:assert/strict";
import { createHash } from "node:crypto";

import { canon } from "../../../sdk/agentguild_verify.mjs";
import { didKeySigner } from "../../../sdk/agentguild_envelope_client.mjs";
import { createAgentGuildX402PaymentPolicy } from "../../../sdk/integrations/x402_payment_policy.mjs";

const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

function b58encode(bytes) {
  let n = BigInt("0x" + Buffer.from(bytes).toString("hex"));
  let out = "";
  while (n > 0n) {
    out = B58[Number(n % 58n)] + out;
    n /= 58n;
  }
  for (const byte of bytes) {
    if (byte === 0) out = "1" + out;
    else break;
  }
  return out || "1";
}

const guild = didKeySigner("55".repeat(32));
const payTo = "0x" + "66".repeat(20);
const asset = "0x" + "77".repeat(20);
const asOf = new Date("2026-08-07T18:00:00Z");

async function secure(unsigned) {
  const proof = {
    "@context": unsigned["@context"],
    type: "DataIntegrityProof",
    cryptosuite: "eddsa-jcs-2022",
    created: unsigned.validFrom,
    verificationMethod: `${guild.did}#${guild.did.slice(8)}`,
    proofPurpose: "assertionMethod",
  };
  const hashData = Buffer.concat([
    createHash("sha256").update(Buffer.from(canon(proof), "utf8")).digest(),
    createHash("sha256").update(Buffer.from(canon(unsigned), "utf8")).digest(),
  ]);
  const signature = await guild.sign(hashData);
  return { ...unsigned, proof: { ...proof, proofValue: "z" + b58encode(signature) } };
}

function response(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const context = {
  paymentRequired: {
    resource: { url: "https://seller.example/research/42" },
  },
  selectedRequirements: {
    scheme: "exact",
    network: "eip155:8453",
    asset,
    amount: "25000",
    payTo,
    maxTimeoutSeconds: 300,
    extra: {},
  },
};

async function decisionFor(body, mutate = null) {
  const subject = {
    id: "did:key:zProvider",
    contract: "AGPD-1/1.0",
    payment: body.payment,
    counterparty: {
      resolution_status: "bound_registered",
      agent: {
        id: "agent_provider",
        did: "did:key:zProvider",
        capabilities: ["fact-check"],
      },
    },
    policy: {
      effective: {
        max_risk: Math.min(body.policy.max_risk, 32.99),
        min_confidence: Math.max(body.policy.min_confidence, 0.5),
      },
    },
    decision: "allow",
    reason: "exact signed allow",
  };
  if (mutate) mutate(subject);
  return secure({
    "@context": ["https://www.w3.org/ns/credentials/v2"],
    id: "urn:agent-guild:payment-decision:test",
    type: ["VerifiableCredential", "AgentGuildPaymentDecision"],
    issuer: guild.did,
    validFrom: "2026-08-07T17:59:00.000Z",
    validUntil: "2026-08-07T18:04:00.000Z",
    credentialSubject: subject,
  });
}

const freeFetch = async url => {
  assert.equal(new URL(url).pathname, "/.well-known/agent-guild-did.json");
  return response({ did: guild.did });
};
let paidCalls = 0;
const paidFetch = async (url, init) => {
  paidCalls += 1;
  assert.equal(new URL(url).pathname, "/wallet-binding/decision");
  const body = JSON.parse(init.body);
  return response(await decisionFor(body));
};

const hook = createAgentGuildX402PaymentPolicy({
  host: "https://guild.example",
  fetchImpl: freeFetch,
  meteredFetch: paidFetch,
  capability: "fact-check",
  maxRisk: 40,
  minConfidence: 0.7,
  now: () => asOf,
});
assert.equal(await hook(context), undefined);
assert.equal(paidCalls, 1);

const tamperedHook = createAgentGuildX402PaymentPolicy({
  host: "https://guild.example",
  fetchImpl: freeFetch,
  meteredFetch: async (_url, init) => {
    const body = JSON.parse(init.body);
    return response(await decisionFor(
      body, subject => { subject.payment.amount = "25001"; }));
  },
  now: () => asOf,
});
const tampered = await tamperedHook(context);
assert.equal(tampered.abort, true);
assert.match(tampered.reason, /invalid, stale, inexact/);

const unpaid = await createAgentGuildX402PaymentPolicy({
  host: "https://guild.example",
  fetchImpl: freeFetch,
  meteredFetch: async () => response({ error: "payment required" }, 402),
  now: () => asOf,
})(context);
assert.equal(unpaid.abort, true);
assert.match(unpaid.reason, /requires payment/);

let shouldNotPay = 0;
const tooLarge = await createAgentGuildX402PaymentPolicy({
  fetchImpl: freeFetch,
  meteredFetch: async () => { shouldNotPay += 1; return response({}); },
  maxAmountAtomic: "24999",
})(context);
assert.equal(tooLarge.abort, true);
assert.match(tooLarge.reason, /maxAmountAtomic/);
assert.equal(shouldNotPay, 0);

assert.throws(
  () => createAgentGuildX402PaymentPolicy({ fetchImpl: paidFetch, meteredFetch: paidFetch }),
  /separate unguarded x402 transport/
);

console.log("x402 payment policy: signed allow, tamper, unpaid, local cap, recursion guard ok");
