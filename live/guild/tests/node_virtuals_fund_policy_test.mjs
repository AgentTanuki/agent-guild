import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { canon } from "../../../sdk/agentguild_verify.mjs";
import { didKeySigner } from "../../../sdk/agentguild_envelope_client.mjs";
import {
  createAgentGuildAcpPaymentPolicy,
  createAgentGuildFundPolicy,
} from "../../../sdk/integrations/virtuals_acp_fund_policy.mjs";

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
const address = "0x" + "66".repeat(20);
const asset = "0x" + "77".repeat(20);
const buyerAddress = "0x" + "88".repeat(20);
const asOf = new Date("2026-08-07T12:00:00Z");

async function signed(body) {
  return {
    ...body,
    proof: Buffer.from(await guild.sign(Buffer.from(canon(body), "utf8"))).toString("hex"),
  };
}

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

const credential = await signed({
  type: "AgentGuildWalletBinding",
  protocol: "agent-guild/wallet-binding/v1",
  credential_id: "wbc_test",
  did: "did:key:z6MkProvider",
  address,
  network: "eip155:8453",
  issued_at: "2026-08-07T11:00:00Z",
  expires_at: "2026-08-08T12:00:00Z",
  issuer: guild.did,
  challenge_nonce: "nonce",
});
const status = await signed({
  type: "AgentGuildWalletBindingStatus",
  protocol: "agent-guild/wallet-binding/v1",
  credential_id: "wbc_test",
  status: "active",
  superseded_by: null,
  revoked_at: null,
  credential_expires_at: "2026-08-08T12:00:00Z",
  as_of: asOf.toISOString(),
  issuer: guild.did,
  note: "live status",
});

function resolution(bindingCredential = credential) {
  return {
    status: "bound_registered",
    address,
    network: "eip155:8453",
    binding: { credential: bindingCredential, status },
    agent: {
      id: "agent_provider",
      did: "did:key:z6MkProvider",
      capabilities: ["fact-check"],
    },
  };
}

function response(body, statusCode = 200) {
  return new Response(JSON.stringify(body), {
    status: statusCode,
    headers: { "content-type": "application/json" },
  });
}

const fetchImpl = async (url) => {
  const path = new URL(url).pathname;
  if (path === "/.well-known/agent-guild-did.json") return response({ did: guild.did });
  if (path === "/wallet-binding/resolve") return response(resolution());
  throw new Error(`unexpected free fetch: ${path}`);
};
const meteredFetch = async (url) => {
  assert.equal(new URL(url).pathname, "/agents/agent_provider/risk-score");
  return response({ recommendation: "hire", risk: 12, confidence: 0.9 });
};
const context = { chainId: 8453, providerAddress: address };

const policy = createAgentGuildFundPolicy({
  host: "https://guild.example",
  fetchImpl,
  meteredFetch,
  capability: "fact-check",
  now: () => asOf,
});
const allowed = await policy(context);
assert.equal(allowed.allow, true);
assert.equal(allowed.evidence.address, address);

const tamperedFetch = async (url) => {
  const path = new URL(url).pathname;
  if (path === "/.well-known/agent-guild-did.json") return response({ did: guild.did });
  if (path === "/wallet-binding/resolve") {
    return response(resolution({ ...credential, address: "0x" + "77".repeat(20) }));
  }
  throw new Error(`unexpected free fetch: ${path}`);
};
const tampered = await createAgentGuildFundPolicy({
  host: "https://guild.example",
  fetchImpl: tamperedFetch,
  meteredFetch,
  now: () => asOf,
})(context);
assert.equal(tampered.allow, false);
assert.match(tampered.reason, /invalid, stale, expired, or not exact/);

const unpaid = await createAgentGuildFundPolicy({
  host: "https://guild.example",
  fetchImpl,
  meteredFetch: async () => response({ error: "payment required" }, 402),
  now: () => asOf,
})(context);
assert.equal(unpaid.allow, false);
assert.match(unpaid.reason, /requires payment/);

const paymentContext = {
  chainId: 8453,
  jobId: 42n,
  providerAddress: address,
  amount: { address: asset, rawAmount: 25000n },
  job: { description: "fact-check" },
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
    id: "urn:agent-guild:payment-decision:acp-test",
    type: ["VerifiableCredential", "AgentGuildPaymentDecision"],
    issuer: guild.did,
    validFrom: "2026-08-07T11:59:00.000Z",
    validUntil: "2026-08-07T12:04:00.000Z",
    credentialSubject: subject,
  });
}

const issuerFetch = async url => {
  assert.equal(new URL(url).pathname, "/.well-known/agent-guild-did.json");
  return response({ did: guild.did });
};
let paymentDecisionCalls = 0;
const decisionFetch = async (url, init) => {
  paymentDecisionCalls += 1;
  assert.equal(new URL(url).pathname, "/wallet-binding/decision");
  const body = JSON.parse(init.body);
  return response(await decisionFor(body));
};

const paymentPolicy = createAgentGuildAcpPaymentPolicy({
  host: "https://guild.example",
  fetchImpl: issuerFetch,
  meteredFetch: decisionFetch,
  resource: context => `https://acp.example/jobs/${context.jobId}`,
  capability: "fact-check",
  maxRisk: 40,
  minConfidence: 0.7,
  now: () => asOf,
});
const paymentAllowed = await paymentPolicy(paymentContext);
assert.equal(paymentAllowed.allow, true);
assert.equal(paymentAllowed.evidence.decision.credentialSubject.payment.amount, "25000");
assert.equal(paymentDecisionCalls, 1);

const tamperedPayment = await createAgentGuildAcpPaymentPolicy({
  host: "https://guild.example",
  fetchImpl: issuerFetch,
  meteredFetch: async (_url, init) => {
    const body = JSON.parse(init.body);
    return response(await decisionFor(
      body, subject => { subject.payment.amount = "25001"; }));
  },
  resource: "https://acp.example/jobs/42",
  now: () => asOf,
})(paymentContext);
assert.equal(tamperedPayment.allow, false);
assert.match(tamperedPayment.reason, /invalid, stale, inexact/);

let shouldNotPay = 0;
const missingResource = await createAgentGuildAcpPaymentPolicy({
  fetchImpl: issuerFetch,
  meteredFetch: async () => { shouldNotPay += 1; return response({}); },
})(paymentContext);
assert.equal(missingResource.allow, false);
assert.match(missingResource.reason, /resource must resolve/);
assert.equal(shouldNotPay, 0);

const unpaidDecision = await createAgentGuildAcpPaymentPolicy({
  host: "https://guild.example",
  fetchImpl: issuerFetch,
  meteredFetch: async () => response({ error: "payment required" }, 402),
  resource: "https://acp.example/jobs/42",
  now: () => asOf,
})(paymentContext);
assert.equal(unpaidDecision.allow, false);
assert.match(unpaidDecision.reason, /requires payment/);

const protectedPaymentContext = {
  ...paymentContext,
  amount: { address: asset, rawAmount: 100_000_000n },
};
const protectedSigner = {
  address: buyerAddress,
  async signMessage() { return "0x" + "11".repeat(65); },
};
const protectedAcp = createAgentGuildAcpPaymentPolicy({
  host: "https://guild.example",
  fetchImpl: issuerFetch,
  meteredFetch: async (url, init) => {
    assert.equal(new URL(url).pathname, "/wallet-binding/protected-decision");
    assert.ok(init.headers["X-Guild-Caller-Proof"]);
    const body = JSON.parse(init.body);
    return response(await decisionFor(body, subject => {
      subject.protection = {
        contract: "agent-guild/protected-value-policy/v1",
        pricing: {
          contract: "agent-guild/protected-value-pricing/v1",
          basis_points: 25,
          minimum_fee_credits: 10,
          maximum_fee_credits: 10_000_000,
          protected_amount_atomic: body.payment.amount,
          fee_credits: 250,
        },
        required_value_tier: "medium",
        value_at_risk: { tiers: { medium: true } },
        reachability: { recommended_for_routing: true },
        service_client: {
          caller_did: `did:pkh:eip155:8453:${buyerAddress}`,
          payer_eoa: buyerAddress,
        },
      };
    }));
  },
  resource: "https://acp.example/jobs/42",
  protectedValue: true,
  evmSigner: protectedSigner,
  now: () => asOf,
});
const protectedAllowed = await protectedAcp(protectedPaymentContext);
assert.equal(protectedAllowed.allow, true);

assert.throws(
  () => createAgentGuildAcpPaymentPolicy({
    fetchImpl: issuerFetch,
    meteredFetch: decisionFetch,
    resource: "https://acp.example/jobs/42",
    protectedValue: true,
  }),
  /evmSigner is required/
);

assert.throws(
  () => createAgentGuildAcpPaymentPolicy({
    fetchImpl: issuerFetch,
    apiKey: "sandbox-only",
    resource: "https://acp.example/jobs/42",
    protectedValue: true,
    evmSigner: protectedSigner,
  }),
  /requires meteredFetch/
);

assert.throws(
  () => createAgentGuildAcpPaymentPolicy({
    fetchImpl: decisionFetch,
    meteredFetch: decisionFetch,
    resource: "https://acp.example/jobs/42",
  }),
  /separate unguarded x402 transport/
);

console.log(
  "virtuals ACP fund policy: signed identity/risk, exact AGPD-1 and protected value paths ok"
);
