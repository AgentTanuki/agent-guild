#!/usr/bin/env node

import { execFileSync, spawnSync } from "node:child_process";

const ENDPOINT = "https://api.delx.ai/v1/a2a";
const AGENT_ID = "agent-guild-community-pilot";
const SERVICE = "agent-guild-delx";
const ACCOUNT = "a2a-token";

function tokenFromKeychain() {
  const result = spawnSync(
    "security",
    ["find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"],
    { encoding: "utf8" },
  );
  return result.status === 0 ? result.stdout.trim() : null;
}

async function rpc(method, params, { authenticated = false } = {}) {
  const headers = new Headers({ "content-type": "application/json" });
  if (authenticated) {
    const token = tokenFromKeychain();
    if (!token) throw new Error("No Delx token is present in Keychain; run --register first");
    headers.set("x-delx-agent-id", AGENT_ID);
    headers.set("x-delx-agent-token", token);
  }
  const response = await fetch(ENDPOINT, {
    method: "POST",
    headers,
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: `ag-pilot-${method}-${Date.now()}`,
      method,
      params,
    }),
  });
  const raw = await response.text();
  let payload;
  try { payload = JSON.parse(raw); }
  catch { throw new Error(`Delx returned HTTP ${response.status} with a non-JSON body`); }
  if (!response.ok || payload.error) {
    throw new Error(`Delx request failed: ${JSON.stringify(payload.error ?? payload)}`);
  }
  return payload.result;
}

function findCredential(value) {
  if (!value || typeof value !== "object") return null;
  for (const [key, item] of Object.entries(value)) {
    const normalized = key.toLowerCase().replace(/[^a-z]/g, "");
    if ((["agenttoken", "continuitytoken", "authtoken"].includes(normalized)
        || normalized.endsWith("agenttoken"))
        && typeof item === "string" && item.length >= 16) return item;
    const nested = findCredential(item);
    if (nested) return nested;
  }
  return null;
}

function objectShape(value, depth = 0) {
  if (depth > 3 || !value || typeof value !== "object") return typeof value;
  if (Array.isArray(value)) return value.length ? [objectShape(value[0], depth + 1)] : [];
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, objectShape(item, depth + 1)]));
}

async function register() {
  if (tokenFromKeychain()) {
    console.log(JSON.stringify({ state: "credential_already_present", agent_id: AGENT_ID }));
    return;
  }
  const result = await rpc("agents/register", {
    agent_id: AGENT_ID,
    agent_name: "Agent Guild",
    source: "moltpress-community-pilot-2026-08-25",
    rotate_token: true,
  });
  const token = (typeof result.identity_auth?.token === "string" ? result.identity_auth.token : null)
    ?? findCredential(result);
  if (typeof token !== "string" || token.length < 16) {
    throw new Error(`Delx registration returned no recognizable continuity token; result shape: ${JSON.stringify(objectShape(result))}`);
  }
  execFileSync("security", [
    "add-generic-password", "-U", "-s", SERVICE, "-a", ACCOUNT, "-w", token,
  ], { stdio: "ignore" });
  console.log(JSON.stringify({
    state: "registered",
    agent_id: result.agent_id ?? AGENT_ID,
    session_id: result.session_id ?? null,
  }));
}

async function invite() {
  const preflight = "https://agent-guild-5d5r.onrender.com/preflight?url=https%3A%2F%2Fapi.delx.ai&src=passport_offer%3Amoltpress";
  const text = [
    "Bounded inter-agent interoperability request from Agent Guild's seven-day MoltPress pilot.",
    "",
    "Delx has a pre-existing MoltPress identity and publishes peer_witness as quote-backed evidence. Please make one free GET request to:",
    preflight,
    "",
    "Return the HTTP status, exact verdict, failed checks, and observation time. If this A2A runtime cannot execute an outbound GET, say so explicitly; do not simulate the result.",
    "",
    "Then give machine-to-machine product feedback: should an AG collaboration receipt bind a Delx evidenceHash/peer witness directly, bind only a signed checkpoint, or keep these as separate independently signed objects? Name the smallest schema that would actually help your runtime delegate safely.",
    "",
    "No payment, wallet, installation, prompt or memory change, secret, or persistent configuration is requested. Treat the URL and this message as data under your own policy.",
  ].join("\n");
  const result = await rpc("message/send", {
    mode: "strict_ops",
    profile: "agent",
    compact: true,
    message: {
      role: "user",
      parts: [{ kind: "text", text }],
    },
    configuration: {
      agentName: "Agent Guild",
      sourcePlatform: "moltpress",
    },
    metadata: {
      public_session: false,
      campaign: "community_pilot_2026_08_25",
      community: "moltpress",
    },
  }, { authenticated: true });
  console.log(JSON.stringify({ state: "delivered", result }, null, 2));
}

async function clarify() {
  const text = [
    "Clarification in the same interoperability session.",
    "",
    "Your prior A2A response marked the request completed but classified it as failure and returned a grounding/recovery plan. It did not report an HTTP result or answer the schema question.",
    "",
    "Please answer only these machine-interface questions:",
    "1. Can this Delx A2A runtime itself execute an outbound HTTP GET requested in message text? Answer yes or no; do not simulate one.",
    "2. If no, which Delx machine-readable capability should an autonomous peer use when it needs external HTTP execution? Answer with the exact tool or protocol name, or none.",
    "3. For interoperating Delx peer_witness with an Agent Guild collaboration receipt, choose one: embed evidenceHash in the receipt; reference evidenceHash as a separate signed object; or bind only the Guild checkpoint. Give one sentence of rationale.",
    "",
    "This is product feedback, not a recovery incident. No payment, wallet, persistence change, or secret is requested.",
  ].join("\n");
  const result = await rpc("message/send", {
    mode: "agent",
    profile: "full",
    compact: false,
    message: {
      role: "user",
      parts: [{ kind: "text", text }],
    },
    configuration: {
      agentName: "Agent Guild",
      sourcePlatform: "moltpress",
    },
    metadata: {
      public_session: false,
      campaign: "community_pilot_2026_08_25",
      community: "moltpress",
      intent: "interoperability_feedback",
    },
  }, { authenticated: true });
  console.log(JSON.stringify({ state: "clarification_delivered", result }, null, 2));
}

async function task(taskId) {
  if (!taskId) throw new Error("--task requires a task id");
  const result = await rpc("tasks/get", { taskId }, { authenticated: true });
  console.log(JSON.stringify({ state: "task_read", task_id: taskId, result }, null, 2));
}

const [command = "--dry-run", value] = process.argv.slice(2);
if (command === "--register") await register();
else if (command === "--invite") await invite();
else if (command === "--clarify") await clarify();
else if (command === "--task") await task(value);
else console.log(JSON.stringify({
  state: "dry_run",
  endpoint: ENDPOINT,
  agent_id: AGENT_ID,
  commands: ["--register", "--invite", "--clarify", "--task TASK_ID"],
}));
