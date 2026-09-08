/**
 * Tests run WITHOUT pi installed: a fake ExtensionAPI captures registrations
 * and a stub fetch replays fixtures. Set AGENT_GUILD_LIVE=1 to also hit the
 * live free endpoints (/preflight, /check) — no key, no payment.
 *
 *   node --experimental-strip-types --test test/*.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  createExtension,
  extractUrls,
  remoteMcpServers,
  summarisePreflight,
  loadConfig,
  publicEndpoint,
  publicAddress,
  type PreflightResult,
} from "../extensions/agent-guild.ts";

type Handler = (event: any, ctx: any) => any;

function fakePi() {
  const tools = new Map<string, any>();
  const commands = new Map<string, any>();
  const handlers = new Map<string, Handler[]>();
  const notices: string[] = [];
  const pi = {
    registerTool: (t: any) => tools.set(t.name, t),
    registerCommand: (n: string, c: any) => commands.set(n, c),
    on: (ev: string, h: Handler) => handlers.set(ev, [...(handlers.get(ev) ?? []), h]),
  } as any;
  const ctx = { ui: { notify: (m: string) => notices.push(m) }, cwd: process.cwd() };
  return { pi, tools, commands, handlers, notices, ctx };
}

const FIX_BAD: PreflightResult = {
  target: "https://bad.example/mcp",
  verdict: "do_not_delegate",
  headline: "This endpoint did not prove it can do the thing it is listed for.",
  checks: [
    { check: "endpoint_reachable", status: "proven", detail: "something answered over HTTP" },
    { check: "protocol_handshake", status: "failed", detail: "a server answered but proved NO agent protocol" },
    { check: "independent_evidence", status: "unknown", detail: "no independent evidence" },
  ],
  failed: ["protocol_handshake"],
  unknowns: ["independent_evidence"],
  scored: ["endpoint_reachable", "protocol_handshake"],
  limits: "unknowns are excluded from the verdict",
};

function stubFetch(routes: Record<string, (u: URL) => { status: number; body: unknown }>) {
  const calls: { url: URL; headers: Record<string, string> }[] = [];
  const f = (async (input: any, init: any) => {
    const url = input instanceof URL ? input : new URL(String(input));
    calls.push({ url, headers: init?.headers ?? {} });
    const route = routes[url.pathname];
    if (!route) return new Response("not found", { status: 404 });
    const r = route(url);
    return new Response(JSON.stringify(r.body), { status: r.status, headers: { "content-type": "application/json" } });
  }) as unknown as typeof fetch;
  return { f, calls };
}

const noFiles = { readFile: async () => { throw new Error("ENOENT"); }, home: "/nonexistent",
  resolve: async () => [{ address: "93.184.216.34" }] };

test("config: defaults are advisory and never gate the Guild's own host", async () => {
  const cfg = await loadConfig({ ...noFiles, env: {} });
  assert.equal(cfg.gate, "off");
  assert.equal(cfg.scanMcpConfig, false);
  assert.ok(cfg.allowHosts.includes("agent-guild-5d5r.onrender.com"));
  const cfg2 = await loadConfig({ ...noFiles, env: { AGENT_GUILD_GATE: "block", AGENT_GUILD_BASE_URL: "https://guild.example/" } });
  assert.equal(cfg2.gate, "block");
  assert.equal(cfg2.baseUrl, "https://guild.example");
  assert.ok(cfg2.allowHosts.includes("guild.example"));
});

test("registers tools and lifecycle cleanup; scanning and gating are off by default", async () => {
  const { pi, tools, commands, handlers } = fakePi();
  const { f } = stubFetch({});
  await createExtension(pi, { ...noFiles, env: {}, fetch: f });
  assert.deepEqual([...tools.keys()].sort(), ["guild_check", "guild_preflight"]);
  assert.ok(commands.has("guild"));
  assert.deepEqual([...handlers.keys()].sort(), ["session_shutdown", "session_start"]);
  for (const t of tools.values()) {
    assert.ok(t.promptSnippet, `${t.name} needs a promptSnippet so it appears in Available tools`);
    assert.ok(t.parameters, `${t.name} needs a typebox schema`);
  }
});

test("guild_preflight: calls /preflight?url=…, identifies itself, summarises checks + unknowns", async () => {
  const { pi, tools, ctx } = fakePi();
  const { f, calls } = stubFetch({ "/preflight": () => ({ status: 200, body: FIX_BAD }) });
  await createExtension(pi, { ...noFiles, env: {}, fetch: f });
  const r = await tools.get("guild_preflight").execute("t1", { url: "https://bad.example/mcp" }, undefined, undefined, ctx);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url.searchParams.get("url"), "https://bad.example/mcp");
  assert.match(calls[0].headers["user-agent"], /^pi-agent-guild\/\d+\.\d+\.\d+/);
  assert.ok(!r.isError);
  const text = r.content[0].text as string;
  assert.match(text, /verdict: do_not_delegate/);
  assert.match(text, /\[failed\] protocol_handshake/);
  assert.match(text, /unknowns \(excluded from verdict/);
  assert.equal(r.details.verdict, "do_not_delegate");
});

test("guild_preflight: HTTP error / timeout is an explicit unknown, never a pass", async () => {
  const { pi, tools, ctx } = fakePi();
  const { f } = stubFetch({ "/preflight": () => ({ status: 503, body: { detail: "down" } }) });
  await createExtension(pi, { ...noFiles, env: {}, fetch: f });
  const r = await tools.get("guild_preflight").execute("t1", { url: "https://x.example" }, undefined, undefined, ctx);
  assert.equal(r.isError, true);
  assert.match(r.content[0].text, /treat as unknown, not as safe/);
});

test("guild_check: a 402 challenge is returned as a quote, never paid", async () => {
  const { pi, tools, ctx } = fakePi();
  const { f } = stubFetch({ "/check": () => ({ status: 402, body: { x402Version: 2, error: "PAYMENT-SIGNATURE header is required", accepts: [{ scheme: "exact", network: "eip155:8453", amount: "10000", asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", extra: { name: "USD Coin" } }], resource: { description: "trust read" } } }) });
  await createExtension(pi, { ...noFiles, env: {}, fetch: f });
  const r = await tools.get("guild_check").execute("t1", { capability: "fact-check" }, undefined, undefined, ctx);
  assert.ok(!r.isError);
  assert.equal(r.details.paymentRequired, true);
  assert.match(r.content[0].text, /\$0\.01 USD Coin on eip155:8453/);
  assert.match(r.content[0].text, /never pays/);
});

test("guild_check: passes capability and signed flag through", async () => {
  const { pi, tools, ctx } = fakePi();
  const { f, calls } = stubFetch({ "/check": (u) => ({ status: 200, body: { capability: u.searchParams.get("capability"), signed: u.searchParams.get("signed") } }) });
  await createExtension(pi, { ...noFiles, env: {}, fetch: f });
  const r = await tools.get("guild_check").execute("t1", { capability: "fact-check", signed: true }, undefined, undefined, ctx);
  assert.equal(calls[0].url.searchParams.get("capability"), "fact-check");
  assert.equal(calls[0].url.searchParams.get("signed"), "true");
  assert.equal(r.details.capability, "fact-check");
});

test("gate=block: only the exact observed endpoint is blocked; unrelated paths pass", async () => {
  const { pi, tools, handlers, ctx, notices } = fakePi();
  const { f } = stubFetch({ "/preflight": () => ({ status: 200, body: FIX_BAD }) });
  await createExtension(pi, { ...noFiles, env: { AGENT_GUILD_GATE: "block" }, fetch: f });
  const [onToolCall] = handlers.get("tool_call")!;
  // Nothing known yet → no block.
  assert.equal(await onToolCall({ toolName: "bash", input: { command: "curl https://bad.example/mcp" } }, ctx), undefined);
  await tools.get("guild_preflight").execute("t1", { url: "https://bad.example/mcp" }, undefined, undefined, ctx);
  assert.equal(await onToolCall({ toolName: "fetch", input: { url: "https://bad.example/docs" } }, ctx), undefined);
  assert.equal(await onToolCall({ toolName: "fetch", input: { url: "https://bad.example/mcp/tools" } }, ctx), undefined);
  const res = await onToolCall({ toolName: "bash", input: { command: "curl -X POST https://bad.example/mcp" } }, ctx);
  assert.equal(res?.block, true);
  assert.match(res.reason, /do_not_delegate/);
  // The Guild's own host and localhost are never gated.
  assert.equal(await onToolCall({ toolName: "fetch", input: { url: "https://agent-guild-5d5r.onrender.com/check?capability=x" } }, ctx), undefined);
  assert.equal(await onToolCall({ toolName: "fetch", input: { url: "http://localhost:3000/mcp" } }, ctx), undefined);
  // guild_* tools are never self-gated.
  assert.equal(await onToolCall({ toolName: "guild_preflight", input: { url: "https://bad.example/mcp" } }, ctx), undefined);
  assert.equal(notices.length, 0);
});

test("gate=warn: notifies instead of blocking", async () => {
  const { pi, tools, handlers, ctx, notices } = fakePi();
  const { f } = stubFetch({ "/preflight": () => ({ status: 200, body: FIX_BAD }) });
  await createExtension(pi, { ...noFiles, env: { AGENT_GUILD_GATE: "warn" }, fetch: f });
  await tools.get("guild_preflight").execute("t1", { url: "https://bad.example/mcp" }, undefined, undefined, ctx);
  const res = await handlers.get("tool_call")![0]({ toolName: "bash", input: { command: "curl https://bad.example/mcp" } }, ctx);
  assert.equal(res, undefined);
  assert.equal(notices.length, 1);
});

test("scanMcpConfig: preflights remote HTTP servers from pi-mcp-adapter config, advisory only", async () => {
  const { pi, handlers, ctx, notices } = fakePi();
  const mcpJson = JSON.stringify({ mcpServers: {
    docs: { url: "https://mcp.docs.example/mcp" },
    local: { command: "npx", args: ["x"] },
    off: { url: "https://off.example/mcp", disabled: true },
  } });
  const { f, calls } = stubFetch({ "/preflight": (u) => ({ status: 200, body: { ...FIX_BAD, target: u.searchParams.get("url") } }) });
  await createExtension(pi, {
    ...noFiles, env: { AGENT_GUILD_SCAN_MCP: "1" }, fetch: f, home: "/h",
    readFile: async (p: string) => { if (p === "/h/.pi/agent/mcp.json") return mcpJson; throw new Error("ENOENT"); },
  });
  for (const handler of handlers.get("session_start")!) await handler({ reason: "startup" }, ctx);
  assert.equal(calls.length, 1, "stdio and disabled servers are not preflighted");
  assert.equal(calls[0].url.searchParams.get("url"), "https://mcp.docs.example/mcp");
  assert.equal(notices.length, 1);
  assert.match(notices[0], /docs: do_not_delegate/);
  assert.equal(handlers.has("tool_call"), false, "scan never gates");
});

test("helpers: extractUrls and remoteMcpServers", () => {
  assert.deepEqual(extractUrls({ a: "see https://a.example/x and http://b.example", b: ["https://c.example/"] }),
    ["https://a.example/x", "http://b.example", "https://c.example/"]);
  assert.deepEqual(remoteMcpServers({ mcpServers: { a: { url: "https://a/mcp" }, b: { command: "x" }, c: { url: "ws://c" } } }), [{ name: "a", url: "https://a/mcp" }]);
  assert.deepEqual(remoteMcpServers(null), []);
  assert.match(summarisePreflight(FIX_BAD), /limits: unknowns are excluded/);
});

test("live: free /preflight and /check answer (no key, no payment)", { skip: !process.env.AGENT_GUILD_LIVE && "set AGENT_GUILD_LIVE=1" }, async () => {
  assert.ok(process.env.GUILD_FIRST_PARTY_TOKEN, "Maintainer live tests require GUILD_FIRST_PARTY_TOKEN so they cannot be mistaken for external adoption");
  const firstPartyFetch: typeof fetch = (input, init) => {
    const headers = new Headers(init?.headers);
    headers.set("X-Agent-Guild-First-Party", process.env.GUILD_FIRST_PARTY_TOKEN!);
    headers.set("X-Agent-Guild-Role", "ops-verify");
    return fetch(input, { ...init, headers });
  };
  const { pi, tools, ctx } = fakePi();
  await createExtension(pi, { ...noFiles, env: {}, fetch: firstPartyFetch });
  const p = await tools.get("guild_preflight").execute("t1", { url: "https://agent-guild-5d5r.onrender.com/a2a" }, undefined, undefined, ctx);
  assert.ok(!p.isError, p.content[0].text);
  assert.ok(["delegate", "no_failed_checks", "delegate_with_caution", "do_not_delegate"].includes(p.details.verdict), p.details.verdict);
  assert.ok(Array.isArray(p.details.checks) && p.details.checks.length >= 3);
  // /check is a paid x402 read: the tool must surface the quote and must not pay.
  const c = await tools.get("guild_check").execute("t2", { capability: "fact-check" }, undefined, undefined, ctx);
  assert.ok(!c.isError, c.content[0].text);
  assert.equal(c.details.paymentRequired, true, "expected a 402 quote for an unsigned /check");
  assert.match(c.content[0].text, /never pays/);
  assert.match(c.content[0].text, /quote: \$0\.\d\d USD Coin on eip155:8453/);
});

test("verdict contract matches public preflight.run (f541d61): no `delegate` verdict exists", async () => {
  const { pi, tools, ctx } = fakePi();
  const { f } = stubFetch({ "/preflight": () => ({ status: 200, body: { ...FIX_BAD, target: "https://bad.example/mcp", verdict: "delegate" } }) });
  await createExtension(pi, { ...noFiles, env: {}, fetch: f, resolve: async () => [{ address: "93.184.216.34" }] });
  const r = await tools.get("guild_preflight").execute("t1", { url: "https://bad.example/mcp" }, undefined, undefined, ctx);
  assert.equal(r.isError, true, "an unknown verdict name must be rejected as malformed, never cached");
  const ok = stubFetch({ "/preflight": () => ({ status: 200, body: { ...FIX_BAD, verdict: "no_failed_checks", failed: [], checks: FIX_BAD.checks.map(c => c.check === "protocol_handshake" ? { ...c, status: "proven" } : c) } }) });
  const { pi: pi2, tools: tools2 } = fakePi();
  await createExtension(pi2, { ...noFiles, env: {}, fetch: ok.f, resolve: async () => [{ address: "93.184.216.34" }] });
  const r2 = await tools2.get("guild_preflight").execute("t2", { url: "https://bad.example/mcp" }, undefined, undefined, ctx);
  assert.equal(r2.details.verdict, "no_failed_checks");
});
