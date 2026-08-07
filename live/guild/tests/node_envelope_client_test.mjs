import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  canon,
  verifyMachineEnvelope,
} from "../../../sdk/agentguild_verify.mjs";
import {
  CALLER_PROOF_HEADER,
  EVM_CALLER_PROOF_PROTOCOL,
  createCallerProof,
  didKeySigner,
  evmWalletCallerProofSigner,
  issueMachineEnvelope,
} from "../../../sdk/agentguild_envelope_client.mjs";

const caller = didKeySigner("11".repeat(32));
const guild = didKeySigner("22".repeat(32));
const payload = JSON.stringify({ action: "delegate", task: "42" });
let paidCalls = 0;

const fetchImpl = async (url) => {
  assert.equal(new URL(url).pathname, "/.well-known/agent-guild-did.json");
  return new Response(JSON.stringify({ did: guild.did }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
};

const paidFetch = async (url, init) => {
  paidCalls += 1;
  assert.equal(new URL(url).pathname, "/envelopes/issue");
  assert.equal(init.method, "POST");
  const body = JSON.parse(init.body);
  assert.equal(body.payload, undefined);
  assert.equal(
    body.payload_sha256,
    createHash("sha256").update(payload).digest("hex"),
  );

  const encoded = new Headers(init.headers).get(CALLER_PROOF_HEADER);
  const proof = JSON.parse(Buffer.from(encoded, "base64").toString("utf8"));
  assert.equal(proof.payload.did, caller.did);
  assert.equal(proof.payload.resource, "/envelopes/issue");
  assert.equal(
    proof.payload.body_sha256,
    createHash("sha256").update(init.body).digest("hex"),
  );

  const issued = new Date();
  const signed = {
    type: "AgentGuildMachineEnvelope",
    version: 1,
    id: "urn:sha256:" + "ab".repeat(32),
    protocol: "agent-guild/machine-envelope/v1",
    issuer: guild.did,
    issued_at: issued.toISOString(),
    valid_until: new Date(issued.getTime() + 3_600_000).toISOString(),
    sender: {
      did: caller.did,
      authentication: "agent-guild/caller-proof/v1",
      caller_proof_verified: true,
    },
    message: {
      kind: body.kind,
      recipient: body.recipient,
      payload_sha256: body.payload_sha256,
      nonce: body.nonce,
    },
    attestation_scope: { attested: "test", not_attested: [], privacy: "test" },
    verification: { suite: "eddsa-jcs-2022" },
  };
  const signature = Buffer.from(
    await guild.sign(Buffer.from(canon(signed), "utf8")),
  ).toString("hex");
  const envelope = { ...signed, proof: signature };
  envelope.envelope_sha256 = createHash("sha256")
    .update(canon(envelope)).digest("hex");
  assert.equal(
    verifyMachineEnvelope(envelope, { expectedIssuer: guild.did }).valid,
    true,
  );
  return new Response(JSON.stringify(envelope), {
    status: 200,
    headers: {
      "content-type": "application/json",
      "PAYMENT-RESPONSE": Buffer.from("settled").toString("base64"),
    },
  });
};

const exactBody = JSON.stringify({ a: 1, b: "two" });
const proof = await createCallerProof({
  signer: caller,
  body: exactBody,
  nonce: "proof-nonce-0001",
  now: new Date("2026-08-07T10:00:00Z"),
});
assert.equal(proof.payload.did, caller.did);
assert.equal(
  proof.payload.body_sha256,
  createHash("sha256").update(exactBody).digest("hex"),
);

let evmSignedBytes;
const evmCaller = evmWalletCallerProofSigner({
  address: "0x" + "33".repeat(20),
  async signMessage({ message }) {
    evmSignedBytes = Buffer.from(message.raw);
    return "0x" + "44".repeat(65);
  },
});
const evmProof = await createCallerProof({
  signer: evmCaller,
  body: exactBody,
  nonce: "evm-proof-nonce-0001",
  now: new Date("2026-08-07T10:00:00Z"),
});
assert.equal(evmProof.payload.v, EVM_CALLER_PROOF_PROTOCOL);
assert.equal(
  evmProof.payload.did,
  "did:pkh:eip155:8453:0x" + "33".repeat(20),
);
assert.equal(evmProof.signature, "0x" + "44".repeat(65));
assert.equal(evmSignedBytes.toString("utf8"), canon(evmProof.payload));

const result = await issueMachineEnvelope({
  signer: caller,
  paidFetch,
  fetchImpl,
  host: "https://agent-guild.example",
  payload,
  kind: "delegation",
  recipient: "did:key:z6MkRecipient",
  nonce: "message-nonce-0001",
});
assert.equal(paidCalls, 1);
assert.equal(result.verification.valid, true);
assert.equal(result.verification.senderDid, caller.did);
assert.ok(result.paymentResponse);
console.log("agentguild_envelope_client: ok");
