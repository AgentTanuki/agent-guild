// Agent Guild non-custodial requester adapter for Taskmarket.
//
// The adapter turns delegation into two separately verifiable acts:
//   1. the buyer purchases a Guild-signed machine envelope over the exact task
//      plan after a fresh application-supplied approval; and
//   2. an AGSM-1 mandate authorizes the exact Taskmarket x402 funding payment
//      immediately before the caller-owned wallet signs it.
//
// It never accepts or rejects submissions, never reads a private key, never
// retries an indeterminate funding request, and never treats a 402 quote as a
// completed payment.

import { createHash, randomUUID } from "node:crypto";

import {
  evmWalletCallerProofSigner,
  issueMachineEnvelope,
} from "../agentguild_envelope_client.mjs";
import { DEFAULT_HOST, canon } from "../agentguild_verify.mjs";
import {
  createAgentGuildX402PaymentPolicy,
} from "./x402_payment_policy.mjs";

export const TASKMARKET_API = "https://api.taskmarket.dev/api";
export const TASKMARKET_NETWORK = "eip155:8453";
export const BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913";

const MAX_DESCRIPTION_CHARS = 20_000;
const MAX_DURATION_HOURS = 24 * 365;
const MODES = new Set(["bounty", "claim", "pitch", "benchmark", "auction"]);

function sha256Hex(value) {
  return createHash("sha256").update(String(value), "utf8").digest("hex");
}

function exactAddress(value, label) {
  const out = String(value || "").toLowerCase();
  if (!/^0x[0-9a-f]{40}$/.test(out)) {
    throw new TypeError(`${label} must be an exact EVM address`);
  }
  return out;
}

function positiveAtomic(value, label) {
  const out = String(value ?? "");
  if (!/^[0-9]+$/.test(out) || BigInt(out) <= 0n) {
    throw new TypeError(`${label} must be a positive atomic-unit integer string`);
  }
  return out;
}

function boundedString(value, label, maximum) {
  const out = String(value || "").trim();
  if (!out || out.length > maximum) {
    throw new TypeError(`${label} must be 1..${maximum} characters`);
  }
  return out;
}

function stringList(values, label, { minimum = 0, maximum = 20 } = {}) {
  if (!Array.isArray(values) || values.length < minimum || values.length > maximum) {
    throw new TypeError(`${label} must contain ${minimum}..${maximum} strings`);
  }
  return values.map((value, index) => boundedString(
    value, `${label}[${index}]`, 500,
  ));
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function markerFor(digest) {
  return `Agent-Guild-Delegation-Digest: ${digest}`;
}

function responseJson(response) {
  return response.text().then(text => {
    if (!text) return null;
    try { return JSON.parse(text); } catch { return { raw: text }; }
  });
}

function items(payload, key) {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload?.[key])) return payload[key];
  if (Array.isArray(payload?.data?.[key])) return payload.data[key];
  if (Array.isArray(payload?.data)) return payload.data;
  return [];
}

async function publicJson(fetchImpl, url, label) {
  const response = await fetchImpl(url, {
    headers: {
      accept: "application/json",
      "user-agent": "agent-guild-taskmarket-requester/2.4.0",
    },
  });
  const body = await responseJson(response);
  if (!response.ok) {
    throw new Error(`${label} failed: HTTP ${response.status}`);
  }
  return body;
}

/**
 * Produce the exact approval object before any payment can occur.
 *
 * `deliverables` are appended to the public Taskmarket description so a
 * requester cannot approve one acceptance contract and fund another.
 */
export function prepareTaskmarketDelegation({
  requesterAddress,
  description,
  rewardAtomic,
  durationHours,
  deliverables,
  tags = [],
  mode = "bounty",
  taskVisibility = "public",
  now = () => new Date(),
  approvalTtlSeconds = 600,
  approvalId = `agtm-approval-${randomUUID()}`,
  machineEnvelopePriceAtomic,
} = {}) {
  const requester = exactAddress(requesterAddress, "requesterAddress");
  const reward = positiveAtomic(rewardAtomic, "rewardAtomic");
  const envelopePrice = positiveAtomic(
    machineEnvelopePriceAtomic, "machineEnvelopePriceAtomic",
  );
  const cleanDescription = boundedString(
    description, "description", MAX_DESCRIPTION_CHARS,
  );
  const cleanDeliverables = stringList(deliverables, "deliverables", {
    minimum: 1,
  });
  const cleanTags = stringList(tags, "tags", { maximum: 20 });
  const selectedMode = String(mode || "");
  if (!MODES.has(selectedMode)) throw new TypeError("unsupported Taskmarket mode");
  if (taskVisibility !== "public") {
    throw new TypeError("version 1 supports public Taskmarket tasks only");
  }
  if (!Number.isInteger(durationHours)
      || durationHours < 1 || durationHours > MAX_DURATION_HOURS) {
    throw new TypeError(`durationHours must be an integer in [1, ${MAX_DURATION_HOURS}]`);
  }
  if (!Number.isInteger(approvalTtlSeconds)
      || approvalTtlSeconds < 60 || approvalTtlSeconds > 3600) {
    throw new TypeError("approvalTtlSeconds must be an integer in [60, 3600]");
  }
  const clock = now();
  if (!(clock instanceof Date) || !Number.isFinite(clock.getTime())) {
    throw new TypeError("now() must return a valid Date");
  }
  const createdAt = clock.toISOString();
  const approvalExpiresAt = new Date(
    clock.getTime() + approvalTtlSeconds * 1000,
  ).toISOString();
  const expectedDeadline = new Date(
    clock.getTime() + durationHours * 3600 * 1000,
  ).toISOString();

  const semanticTask = {
    contract: "agent-guild/taskmarket-delegation/v1",
    requester_address: requester,
    taskmarket_endpoint: `${TASKMARKET_API}/tasks`,
    network: TASKMARKET_NETWORK,
    settlement_asset: BASE_USDC,
    description: cleanDescription,
    reward_atomic: reward,
    agent_guild_envelope_atomic: envelopePrice,
    duration_hours: durationHours,
    expected_deadline: expectedDeadline,
    deliverables: cleanDeliverables,
    tags: cleanTags,
    mode: selectedMode,
    task_visibility: taskVisibility,
    approval_id: approvalId,
    approval_expires_at: approvalExpiresAt,
  };
  const taskDigest = sha256Hex(canon(semanticTask));
  const taskDescription = [
    cleanDescription,
    "",
    "## Acceptance deliverables",
    ...cleanDeliverables.map((item, index) => `${index + 1}. ${item}`),
    "",
    markerFor(taskDigest),
  ].join("\n");
  if (taskDescription.length > MAX_DESCRIPTION_CHARS) {
    throw new TypeError("description plus deliverables exceeds 20000 characters");
  }
  const taskBody = {
    description: taskDescription,
    reward,
    duration: durationHours,
    mode: selectedMode,
    tags: cleanTags,
    taskVisibility,
  };
  const maximumTotal = BigInt(reward) + BigInt(envelopePrice);
  return Object.freeze({
    contract: semanticTask.contract,
    approval_id: approvalId,
    created_at: createdAt,
    approval_expires_at: approvalExpiresAt,
    task_digest_sha256: taskDigest,
    requester_address: requester,
    network: TASKMARKET_NETWORK,
    settlement_asset: BASE_USDC,
    semantic_task: Object.freeze(semanticTask),
    task_body: Object.freeze(taskBody),
    deliverables: Object.freeze(cleanDeliverables),
    expected_deadline: expectedDeadline,
    spend: Object.freeze({
      taskmarket_funding_atomic: reward,
      agent_guild_envelope_atomic: envelopePrice,
      maximum_total_atomic: maximumTotal.toString(),
      maximum_total_usdc: (Number(maximumTotal) / 1_000_000).toFixed(6),
    }),
    requires_fresh_approval: true,
    acceptance_is_never_automatic: true,
  });
}

function assertPlanIntegrity(plan, requester) {
  if (!plan || plan.contract !== "agent-guild/taskmarket-delegation/v1") {
    throw new TypeError("prepare() must produce the task plan");
  }
  const semantic = plan.semantic_task;
  if (!semantic || semantic.contract !== plan.contract) {
    throw new Error("task plan semantic contract is missing or inexact");
  }
  const digest = sha256Hex(canon(semantic));
  if (digest !== plan.task_digest_sha256) {
    throw new Error("task plan digest does not match its exact semantic terms");
  }
  if (plan.requester_address !== requester
      || semantic.requester_address !== requester) {
    throw new Error("task plan requester does not match signer");
  }
  const expectedMaximum = (
    BigInt(semantic.reward_atomic) + BigInt(semantic.agent_guild_envelope_atomic)
  ).toString();
  const task = plan.task_body || {};
  const exact = String(task.reward) === semantic.reward_atomic
    && task.duration === semantic.duration_hours
    && task.mode === semantic.mode
    && task.taskVisibility === semantic.task_visibility
    && JSON.stringify(task.tags) === JSON.stringify(semantic.tags)
    && JSON.stringify(plan.deliverables) === JSON.stringify(semantic.deliverables)
    && plan.expected_deadline === semantic.expected_deadline
    && plan.approval_id === semantic.approval_id
    && plan.approval_expires_at === semantic.approval_expires_at
    && plan.spend?.taskmarket_funding_atomic === semantic.reward_atomic
    && plan.spend?.agent_guild_envelope_atomic
      === semantic.agent_guild_envelope_atomic
    && plan.spend?.maximum_total_atomic === expectedMaximum
    && typeof task.description === "string"
    && task.description.startsWith(`${semantic.description}\n\n`)
    && task.description.includes(markerFor(digest));
  if (!exact) {
    throw new Error("task plan was modified after preview; prepare it again");
  }
}

export class TaskmarketFundingUnknownError extends Error {
  constructor(message, details) {
    super(message);
    this.name = "TaskmarketFundingUnknownError";
    this.retryAllowed = false;
    this.details = details;
  }
}

async function defaultTaskTransport({ fetchImpl, evmSigner, mandateId, rewardAtomic }) {
  let x402Fetch;
  let x402Evm;
  try {
    [x402Fetch, x402Evm] = await Promise.all([
      import("@x402/fetch"),
      import("@x402/evm/exact/client"),
    ]);
  } catch (error) {
    throw new Error(
      "install @x402/fetch and @x402/evm for Taskmarket funding: "
      + (error?.message || error),
    );
  }
  const client = new x402Fetch.x402Client();
  x402Evm.registerExactEvmScheme(client, { signer: evmSigner });
  client.onBeforePaymentCreation(createAgentGuildX402PaymentPolicy({
    fetchImpl,
    evmSigner,
    mandateId,
    maxAmountAtomic: rewardAtomic,
  }));
  return x402Fetch.wrapFetchWithPayment(fetchImpl, client);
}

async function defaultEnvelopeClient({
  fetchImpl, evmSigner, mandateId, envelopePriceAtomic,
}) {
  let x402Fetch;
  let x402Evm;
  try {
    [x402Fetch, x402Evm] = await Promise.all([
      import("@x402/fetch"),
      import("@x402/evm/exact/client"),
    ]);
  } catch (error) {
    throw new Error(
      "install @x402/fetch and @x402/evm for signed delegation approval: "
      + (error?.message || error),
    );
  }
  const client = new x402Fetch.x402Client();
  x402Evm.registerExactEvmScheme(client, { signer: evmSigner });
  client.onBeforePaymentCreation(createAgentGuildX402PaymentPolicy({
    fetchImpl,
    evmSigner,
    mandateId,
    maxAmountAtomic: envelopePriceAtomic,
  }));
  const paidFetch = x402Fetch.wrapFetchWithPayment(fetchImpl, client);
  const callerSigner = evmWalletCallerProofSigner(evmSigner);
  return Object.freeze({
    async issue(options) {
      return issueMachineEnvelope({
        ...options,
        signer: callerSigner,
        paidFetch,
        fetchImpl,
      });
    },
  });
}

async function liveEnvelopePriceAtomic(fetchImpl, guildHost) {
  const manifest = await publicJson(
    fetchImpl,
    new URL("/.well-known/agent-guild.json?src=paid_offer:taskmarket", guildHost),
    "Agent Guild paid catalog",
  );
  const operation = manifest?.paid_operations?.operations?.find(
    item => item?.operation === "machine_envelope",
  );
  const credits = operation?.price_credits;
  if (!Number.isSafeInteger(credits) || credits <= 0) {
    throw new Error("Agent Guild catalog omitted an exact machine-envelope price");
  }
  return (BigInt(credits) * 1_000n).toString();
}

/**
 * Create the non-custodial requester client.
 *
 * `approve(plan)` is mandatory and must return the exact approval id. Wallet
 * material is never accepted; pass a signer object owned by the host runtime.
 * `transportFactory` and `envelopeClient` are injectable for deterministic
 * testing, while production defaults use the official x402 EVM client.
 */
export async function createAgentGuildTaskmarketRequester({
  evmSigner,
  mandateId,
  approve,
  fetchImpl = globalThis.fetch,
  transportFactory = defaultTaskTransport,
  envelopeClient = null,
  taskmarketApi = TASKMARKET_API,
  guildHost = DEFAULT_HOST,
  envelopePriceAtomic = null,
  now = () => new Date(),
} = {}) {
  if (!evmSigner || typeof evmSigner.signMessage !== "function") {
    throw new TypeError("evmSigner with signMessage() is required");
  }
  const requester = exactAddress(evmSigner.address, "evmSigner.address");
  if (!String(mandateId || "").startsWith("agsm_")) {
    throw new TypeError("an AGSM-1 mandateId is required");
  }
  if (typeof approve !== "function") {
    throw new TypeError("approve(plan) is required for fresh explicit authorization");
  }
  if (typeof fetchImpl !== "function" || typeof transportFactory !== "function") {
    throw new TypeError("fetchImpl and transportFactory must be functions");
  }
  const base = String(taskmarketApi).replace(/\/$/, "");
  const exactEnvelopePrice = envelopePriceAtomic === null
    ? await liveEnvelopePriceAtomic(fetchImpl, guildHost)
    : positiveAtomic(envelopePriceAtomic, "envelopePriceAtomic");
  const guildEnvelope = envelopeClient || await defaultEnvelopeClient({
    evmSigner, fetchImpl, mandateId, envelopePriceAtomic: exactEnvelopePrice,
  });

  async function listRequesterTasks() {
    const url = new URL(`${base}/tasks`);
    url.searchParams.set("requester", requester);
    url.searchParams.set("status", "ALL");
    url.searchParams.set("limit", "100");
    return items(await publicJson(fetchImpl, url, "Taskmarket task list"), "tasks");
  }

  async function reconcile(plan) {
    const marker = markerFor(plan.task_digest_sha256);
    const tasks = await listRequesterTasks();
    return tasks.find(task => task?.requester?.toLowerCase() === requester
      && task?.description?.includes(marker)) || null;
  }

  return Object.freeze({
    prepare(options) {
      return prepareTaskmarketDelegation({
        ...options,
        requesterAddress: requester,
        machineEnvelopePriceAtomic: exactEnvelopePrice,
        now,
      });
    },

    async create(plan) {
      assertPlanIntegrity(plan, requester);
      if (new Date(plan.approval_expires_at) < now()) {
        throw new Error("task approval window expired before authorization");
      }
      const approval = await approve(clone(plan));
      if (approval?.approved !== true
          || approval?.approvalId !== plan.approval_id) {
        throw new Error("fresh explicit task approval was not granted");
      }

      // This paid envelope is the durable approval receipt. Only its digest is
      // sent to the Guild; the complete task plan remains with the requester.
      const envelopeResult = await guildEnvelope.issue({
        payload: JSON.stringify(plan),
        payloadMediaType: "application/json",
        kind: "authorization",
        recipient: "https://taskmarket.dev",
        resource: `${base}/tasks`,
        context: {
          contract: plan.contract,
          task_digest_sha256: plan.task_digest_sha256,
          approval_id: plan.approval_id,
        },
      });

      const paidFetch = await transportFactory({
        fetchImpl,
        evmSigner,
        mandateId,
        rewardAtomic: plan.task_body.reward,
      });
      let response;
      try {
        // Exactly one application-level funding attempt. The official x402
        // transport performs the protocol's 402 -> signed retry internally.
        response = await paidFetch(`${base}/tasks`, {
          method: "POST",
          headers: {
            accept: "application/json",
            "content-type": "application/json",
            "user-agent": "agent-guild-taskmarket-requester/2.4.0",
            "x-agent-guild-envelope-id": envelopeResult.envelope?.id || "",
          },
          body: JSON.stringify(plan.task_body),
        });
      } catch (error) {
        const found = await reconcile(plan).catch(() => null);
        if (found) {
          return Object.freeze({
            status: "reconciled_after_indeterminate_response",
            task: found,
            envelope: envelopeResult,
            retry_performed: false,
          });
        }
        throw new TaskmarketFundingUnknownError(
          "Taskmarket funding outcome is unknown; inspect by task digest before any retry",
          {
            task_digest_sha256: plan.task_digest_sha256,
            envelope_id: envelopeResult.envelope?.id || null,
            cause: String(error?.message || error),
          },
        );
      }
      const body = await responseJson(response);
      if (!response.ok) {
        const found = await reconcile(plan).catch(() => null);
        if (found) {
          return Object.freeze({
            status: "reconciled_after_indeterminate_response",
            task: found,
            envelope: envelopeResult,
            retry_performed: false,
          });
        }
        throw new TaskmarketFundingUnknownError(
          `Taskmarket returned HTTP ${response.status} after the x402 transport; inspect by task digest before any retry`,
          {
            task_digest_sha256: plan.task_digest_sha256,
            envelope_id: envelopeResult.envelope?.id || null,
            response: body,
          },
        );
      }
      const taskId = body?.taskId || body?.data?.taskId;
      if (!taskId) {
        throw new TaskmarketFundingUnknownError(
          "Taskmarket response omitted taskId after funding; inspect by task digest before any retry",
          {
            task_digest_sha256: plan.task_digest_sha256,
            envelope_id: envelopeResult.envelope?.id || null,
            response: body,
          },
        );
      }
      let taskData;
      try {
        const task = await publicJson(
          fetchImpl, `${base}/tasks/${taskId}`, "Taskmarket task readback",
        );
        taskData = task?.data || task;
      } catch (error) {
        throw new TaskmarketFundingUnknownError(
          "Taskmarket task was funded but readback is unavailable; do not retry creation",
          {
            task_id: taskId,
            task_digest_sha256: plan.task_digest_sha256,
            envelope_id: envelopeResult.envelope?.id || null,
            cause: String(error?.message || error),
          },
        );
      }
      if (taskData?.requester?.toLowerCase() !== requester
          || String(taskData?.reward) !== String(plan.task_body.reward)
          || !taskData?.description?.includes(markerFor(plan.task_digest_sha256))) {
        throw new TaskmarketFundingUnknownError(
          "Taskmarket readback did not match the approved exact task; do not fund again",
          {
            task_id: taskId,
            task_digest_sha256: plan.task_digest_sha256,
            envelope_id: envelopeResult.envelope?.id || null,
          },
        );
      }
      return Object.freeze({
        status: "created",
        task: taskData,
        envelope: envelopeResult,
        retry_performed: false,
      });
    },

    async getTask(taskId) {
      return publicJson(fetchImpl, `${base}/tasks/${encodeURIComponent(taskId)}`, "Taskmarket task");
    },

    async listSubmissions(taskId) {
      const payload = await publicJson(
        fetchImpl,
        `${base}/tasks/${encodeURIComponent(taskId)}/submissions`,
        "Taskmarket submissions",
      );
      return items(payload, "submissions");
    },

    async review(taskId) {
      const [task, submissions] = await Promise.all([
        this.getTask(taskId),
        this.listSubmissions(taskId),
      ]);
      return Object.freeze({
        task: task?.data || task,
        submissions,
        decision_required: "The requester must explicitly review and accept or reject outside this adapter.",
        automatic_acceptance: false,
        automatic_rejection: false,
      });
    },

    reconcile,
  });
}
