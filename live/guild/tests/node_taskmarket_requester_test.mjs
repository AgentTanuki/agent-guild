import assert from "node:assert/strict";

import {
  BASE_USDC,
  TaskmarketFundingUnknownError,
  createAgentGuildTaskmarketRequester,
  prepareTaskmarketDelegation,
} from "../../../sdk/integrations/taskmarket_requester.mjs";

const address = "0x" + "12".repeat(20);
const signer = {
  address,
  async signMessage() { return "0x" + "34".repeat(65); },
};
const clock = new Date("2026-08-13T06:00:00.000Z");

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const prepared = prepareTaskmarketDelegation({
  requesterAddress: address,
  description: "Audit one public API and return reproducible evidence.",
  rewardAtomic: "2500000",
  durationHours: 24,
  deliverables: ["Markdown report", "Machine-readable findings JSON"],
  tags: ["audit", "agent-guild"],
  approvalId: "agtm-approval-test-1234",
  machineEnvelopePriceAtomic: "10000",
  now: () => clock,
});
assert.equal(prepared.network, "eip155:8453");
assert.equal(prepared.settlement_asset, BASE_USDC);
assert.equal(prepared.spend.maximum_total_atomic, "2510000");
assert.match(prepared.task_body.description, /Acceptance deliverables/);
assert.match(prepared.task_body.description, /Agent-Guild-Delegation-Digest/);
assert.equal(prepared.acceptance_is_never_automatic, true);

let catalogCalls = 0;
const catalogClient = await createAgentGuildTaskmarketRequester({
  evmSigner: signer,
  mandateId: "agsm_test_mandate_1234",
  now: () => clock,
  fetchImpl: async url => {
    assert.equal(new URL(url).pathname, "/.well-known/agent-guild.json");
    catalogCalls += 1;
    return json({ paid_operations: { operations: [{
      operation: "machine_envelope", price_credits: 17,
    }] } });
  },
  approve: async () => ({ approved: false }),
  envelopeClient: { async issue() { throw new Error("must not issue"); } },
  transportFactory: async () => { throw new Error("must not fund"); },
});
const catalogPrepared = catalogClient.prepare({
  description: "Catalog price check",
  rewardAtomic: "1000000",
  durationHours: 1,
  deliverables: ["One report"],
  approvalId: "agtm-approval-catalog-1234",
});
assert.equal(catalogCalls, 1);
assert.equal(catalogPrepared.spend.agent_guild_envelope_atomic, "17000");
assert.equal(catalogPrepared.spend.maximum_total_atomic, "1017000");

const tampered = JSON.parse(JSON.stringify(prepared));
tampered.task_body.reward = "1";

let approvalCalls = 0;
let envelopeCalls = 0;
let fundingCalls = 0;
let transportOptions;
const task = {
  id: "0x" + "ab".repeat(32),
  requester: address,
  reward: "2500000",
  description: prepared.task_body.description,
  status: "open",
};
const publicFetch = async url => {
  const path = new URL(url).pathname;
  if (path.endsWith(`/tasks/${task.id}`)) return json(task);
  if (path.endsWith(`/tasks/${task.id}/submissions`)) {
    return json([{ id: "submission-1", artifacts: [] }]);
  }
  if (path.endsWith("/tasks")) return json({ tasks: [] });
  throw new Error(`unexpected public URL ${url}`);
};
const client = await createAgentGuildTaskmarketRequester({
  evmSigner: signer,
  mandateId: "agsm_test_mandate_1234",
  now: () => clock,
  fetchImpl: publicFetch,
  approve: async plan => {
    approvalCalls += 1;
    assert.equal(plan.spend.maximum_total_atomic, "2510000");
    return { approved: true, approvalId: plan.approval_id };
  },
  envelopeClient: {
    async issue(options) {
      envelopeCalls += 1;
      assert.equal(options.kind, "authorization");
      assert.equal(options.recipient, "https://taskmarket.dev");
      assert.equal(JSON.parse(options.payload).task_digest_sha256,
        prepared.task_digest_sha256);
      return { envelope: { id: "urn:ag:envelope:test" }, verification: { valid: true } };
    },
  },
  envelopePriceAtomic: "10000",
  transportFactory: async options => {
    transportOptions = options;
    return async (_url, init) => {
      fundingCalls += 1;
      assert.deepEqual(JSON.parse(init.body), prepared.task_body);
      return json({ success: true, taskId: task.id });
    };
  },
});
const created = await client.create(prepared);
assert.equal(created.status, "created");
assert.equal(created.task.id, task.id);
assert.equal(approvalCalls, 1);
assert.equal(envelopeCalls, 1);
assert.equal(fundingCalls, 1);
assert.equal(transportOptions.rewardAtomic, "2500000");
assert.equal(transportOptions.mandateId, "agsm_test_mandate_1234");
await assert.rejects(() => client.create(tampered), /modified after preview/);

const review = await client.review(task.id);
assert.equal(review.submissions.length, 1);
assert.equal(review.automatic_acceptance, false);
assert.equal(review.automatic_rejection, false);

const denied = await createAgentGuildTaskmarketRequester({
  evmSigner: signer,
  mandateId: "agsm_test_mandate_1234",
  now: () => clock,
  fetchImpl: publicFetch,
  approve: async () => ({ approved: false, approvalId: prepared.approval_id }),
  envelopeClient: { async issue() { throw new Error("must not issue"); } },
  envelopePriceAtomic: "10000",
  transportFactory: async () => { throw new Error("must not fund"); },
});
await assert.rejects(() => denied.create(prepared), /explicit task approval/);

let indeterminateFundingCalls = 0;
const indeterminate = await createAgentGuildTaskmarketRequester({
  evmSigner: signer,
  mandateId: "agsm_test_mandate_1234",
  now: () => clock,
  fetchImpl: publicFetch,
  approve: async plan => ({ approved: true, approvalId: plan.approval_id }),
  envelopeClient: {
    async issue() { return { envelope: { id: "urn:ag:envelope:unknown" } }; },
  },
  envelopePriceAtomic: "10000",
  transportFactory: async () => async () => {
    indeterminateFundingCalls += 1;
    throw new Error("connection lost after signed request");
  },
});
let unknown;
try {
  await indeterminate.create(prepared);
} catch (error) {
  unknown = error;
}
assert.ok(unknown instanceof TaskmarketFundingUnknownError);
assert.equal(unknown.retryAllowed, false);
assert.equal(indeterminateFundingCalls, 1);

const reconciledTask = { ...task, id: "0x" + "cd".repeat(32) };
const reconcileFetch = async url => {
  const path = new URL(url).pathname;
  if (path.endsWith("/tasks")) return json({ tasks: [reconciledTask] });
  throw new Error(`unexpected reconcile URL ${url}`);
};
const reconciling = await createAgentGuildTaskmarketRequester({
  evmSigner: signer,
  mandateId: "agsm_test_mandate_1234",
  now: () => clock,
  fetchImpl: reconcileFetch,
  approve: async plan => ({ approved: true, approvalId: plan.approval_id }),
  envelopeClient: {
    async issue() { return { envelope: { id: "urn:ag:envelope:reconciled" } }; },
  },
  envelopePriceAtomic: "10000",
  transportFactory: async () => async () => {
    throw new Error("lost response");
  },
});
const reconciled = await reconciling.create(prepared);
assert.equal(reconciled.status, "reconciled_after_indeterminate_response");
assert.equal(reconciled.retry_performed, false);
assert.equal(reconciled.task.id, reconciledTask.id);

const missingReadback = await createAgentGuildTaskmarketRequester({
  evmSigner: signer,
  mandateId: "agsm_test_mandate_1234",
  now: () => clock,
  fetchImpl: async url => {
    if (new URL(url).pathname.endsWith("/tasks")) return json({ tasks: [] });
    throw new Error("readback unavailable");
  },
  approve: async plan => ({ approved: true, approvalId: plan.approval_id }),
  envelopeClient: {
    async issue() { return { envelope: { id: "urn:ag:envelope:readback" } }; },
  },
  envelopePriceAtomic: "10000",
  transportFactory: async () => async () => json({ taskId: "0x" + "ef".repeat(32) }),
});
let readbackError;
try {
  await missingReadback.create(prepared);
} catch (error) {
  readbackError = error;
}
assert.ok(readbackError instanceof TaskmarketFundingUnknownError);
assert.equal(readbackError.retryAllowed, false);
assert.match(readbackError.message, /do not retry/);

console.log("fresh approval, signed intent, exact cap, review and no-blind-retry paths ok");
