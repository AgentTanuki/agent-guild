#!/usr/bin/env node

import { execFileSync, spawnSync } from "node:child_process";

const DIRECTORY = "https://for.you.com";
const AG_CARD = "https://agent-guild-5d5r.onrender.com/.well-known/agent-card.json";
const AGENT_WATCH = "https://for.you.com/api/a2a/agent-watch";
const SERVICE = "agent-guild-foryou";
const ACCOUNT = "bearer-key";
const UA = "SKILL/(agent-guild community-pilot-2026-08-25)";

function credentialFromKeychain() {
  const result = spawnSync(
    "security",
    ["find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"],
    { encoding: "utf8" },
  );
  return result.status === 0 ? result.stdout.trim() : null;
}

function findCredential(value) {
  if (!value || typeof value !== "object") return null;
  for (const [key, item] of Object.entries(value)) {
    const normalized = key.toLowerCase().replace(/[^a-z]/g, "");
    if (["apikey", "bearerkey", "bearertoken", "accesstoken"].includes(normalized)
        && typeof item === "string" && item.length >= 16) return item;
    const nested = findCredential(item);
    if (nested) return nested;
  }
  return null;
}

function publicView(value, depth = 0) {
  if (depth > 4 || !value || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map((item) => publicView(item, depth + 1));
  return Object.fromEntries(Object.entries(value).map(([key, item]) => {
    const normalized = key.toLowerCase().replace(/[^a-z]/g, "");
    if (["apikey", "bearerkey", "bearertoken", "accesstoken"].includes(normalized)) {
      return [key, typeof item === "string" ? `<redacted:${item.length}>` : "<redacted>"];
    }
    return [key, publicView(item, depth + 1)];
  }));
}

async function json(response, label) {
  const raw = await response.text();
  let body;
  try { body = JSON.parse(raw); }
  catch { throw new Error(`${label} returned HTTP ${response.status} with a non-JSON body`); }
  if (!response.ok) throw new Error(`${label} returned HTTP ${response.status}: ${JSON.stringify(publicView(body))}`);
  return body;
}

async function register() {
  if (credentialFromKeychain()) {
    console.log(JSON.stringify({ state: "credential_already_present", card: AG_CARD }));
    return;
  }
  const card = await json(await fetch(AG_CARD, { headers: { "user-agent": UA } }), "Agent Guild card");
  const body = await json(await fetch(`${DIRECTORY}/api/v1/agents`, {
    method: "POST",
    headers: { "content-type": "application/json", "user-agent": UA },
    body: JSON.stringify(card),
  }), "For You registration");
  const credential = findCredential(body);
  if (credential) {
    execFileSync("security", [
      "add-generic-password", "-U", "-s", SERVICE, "-a", ACCOUNT, "-w", credential,
    ], { stdio: "ignore" });
  }
  console.log(JSON.stringify({
    state: credential ? "registered" : "registered_without_returned_credential",
    result: publicView(body),
  }, null, 2));
}

async function verify() {
  const catalog = await json(await fetch(`${DIRECTORY}/.well-known/ai-catalog.json`, {
    headers: { "user-agent": UA },
  }), "For You catalog");
  const matches = (catalog.entries ?? []).filter((entry) => {
    const haystack = JSON.stringify(entry).toLowerCase();
    return haystack.includes("agent guild") || haystack.includes("agent-guild-5d5r");
  });
  console.log(JSON.stringify({ state: matches.length ? "verified" : "not_found", matches }, null, 2));
  if (!matches.length) process.exitCode = 1;
}

async function posts() {
  const payload = {
    jsonrpc: "2.0",
    id: `ag-pilot-agent-watch-${Date.now()}`,
    method: "message/send",
    params: {
      message: {
        parts: [{ type: "data", data: { type: "youagent/posts-request", limit: 20 } }],
      },
    },
  };
  const body = await json(await fetch(AGENT_WATCH, {
    method: "POST",
    headers: { "content-type": "application/json", "user-agent": UA },
    body: JSON.stringify(payload),
  }), "Agent Watch A2A");
  console.log(JSON.stringify({ state: "posts_received", result: body }, null, 2));
}

const command = process.argv[2] ?? "--dry-run";
if (command === "--register") await register();
else if (command === "--verify") await verify();
else if (command === "--posts") await posts();
else console.log(JSON.stringify({
  state: "dry_run",
  directory: DIRECTORY,
  card: AG_CARD,
  commands: ["--register", "--verify", "--posts"],
}));
