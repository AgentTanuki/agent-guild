#!/usr/bin/env node

import { randomUUID } from "node:crypto";

function parseArgs(argv) {
  const out = { endpoint: null, tool: null, arguments: {} };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--endpoint") out.endpoint = argv[++i];
    else if (argv[i] === "--tool") out.tool = argv[++i];
    else if (argv[i] === "--arguments") out.arguments = JSON.parse(argv[++i]);
    else throw new Error(`unknown argument: ${argv[i]}`);
  }
  if (!out.endpoint) throw new Error("--endpoint is required");
  return out;
}

function decodeBody(raw, contentType) {
  if (!raw.trim()) return null;
  if (contentType.includes("text/event-stream")) {
    const messages = raw
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trim())
      .filter(Boolean)
      .map((line) => JSON.parse(line));
    return messages.at(-1) ?? null;
  }
  return JSON.parse(raw);
}

const opts = parseArgs(process.argv.slice(2));
const headers = {
  accept: "application/json, text/event-stream",
  "content-type": "application/json",
};

async function rpc(method, params, { notification = false } = {}) {
  const payload = { jsonrpc: "2.0", method };
  if (!notification) payload.id = randomUUID();
  if (params !== undefined) payload.params = params;
  const response = await fetch(opts.endpoint, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });
  const session = response.headers.get("mcp-session-id");
  if (session) headers["mcp-session-id"] = session;
  const raw = await response.text();
  if (!response.ok) {
    throw new Error(`MCP HTTP ${response.status}: ${raw.slice(0, 1000)}`);
  }
  return decodeBody(raw, response.headers.get("content-type") || "");
}

const initialized = await rpc("initialize", {
  protocolVersion: "2025-06-18",
  capabilities: {},
  clientInfo: {
    name: "agent-guild-community-pilot",
    version: "2026-08-25",
  },
});
await rpc("notifications/initialized", {}, { notification: true });
const listed = await rpc("tools/list", {});

if (!opts.tool) {
  console.log(JSON.stringify({
    mode: "list",
    endpoint: opts.endpoint,
    initialized: initialized?.result ?? initialized,
    tools: listed?.result?.tools ?? listed?.tools ?? [],
  }, null, 2));
  process.exit(0);
}

const tools = listed?.result?.tools ?? listed?.tools ?? [];
if (!tools.some((tool) => tool.name === opts.tool)) {
  throw new Error(`server did not declare requested tool: ${opts.tool}`);
}
const called = await rpc("tools/call", {
  name: opts.tool,
  arguments: opts.arguments,
});
console.log(JSON.stringify({
  mode: "call",
  endpoint: opts.endpoint,
  tool: opts.tool,
  arguments: opts.arguments,
  response: called,
}, null, 2));
