import assert from "node:assert/strict";
import { canon } from "../../../sdk/agentguild_verify.mjs";
import { didKeySigner } from "../../../sdk/agentguild_envelope_client.mjs";
import { createAgentGuildFundPolicy } from "../../../sdk/integrations/virtuals_acp_fund_policy.mjs";

const guild = didKeySigner("55".repeat(32));
const address = "0x" + "66".repeat(20);
const asOf = new Date("2026-08-07T12:00:00Z");

async function signed(body) {
  return {
    ...body,
    proof: Buffer.from(await guild.sign(Buffer.from(canon(body), "utf8"))).toString("hex"),
  };
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

console.log("virtuals ACP fund policy: signed allow, tamper, and unpaid paths ok");
