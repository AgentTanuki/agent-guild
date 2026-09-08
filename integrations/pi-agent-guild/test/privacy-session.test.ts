import { test } from "node:test";
import assert from "node:assert/strict";
import { createExtension, publicEndpoint, publicAddress, GuildClient } from "../extensions/agent-guild.ts";

function setup(overrides: Record<string, any> = {}) {
  const hooks = new Map<string, any[]>(), tools = new Map<string, any>();
  const pi = { registerTool: (x: any) => tools.set(x.name, x), registerCommand: () => {},
    on: (name: string, fn: any) => hooks.set(name, [...(hooks.get(name) ?? []), fn]) } as any;
  const calls: URL[] = [];
  const result = (target: string) => ({ target, verdict: "do_not_delegate",
    checks: [{ check: "protocol_handshake", status: "failed" }], failed: ["protocol_handshake"] });
  const deps = { env: { AGENT_GUILD_GATE: "block" }, home: "/h",
    readFile: async () => { throw new Error("ENOENT"); },
    resolve: async () => [{ address: "93.184.216.34" }],
    fetch: async (url: URL) => { calls.push(url); return Response.json(result(url.searchParams.get("url")!)); },
    ...overrides };
  const ready = createExtension(pi, deps);
  return { hooks, tools, calls, ready, ctx: { cwd: "/project", ui: { notify: () => {} } } };
}

test("private, credential-bearing and query-bearing endpoints are rejected before disclosure", async () => {
  const s = setup(); const { client } = await s.ready;
  for (const url of ["https://user:secret@example.com/mcp", "https://example.com/mcp?key=secret",
    "https://example.com/mcp#secret", "http://127.0.0.1/mcp", "http://0x7f000001/mcp",
    "http://[::ffff:127.0.0.1]/mcp", "http://10.0.0.1/mcp", "http://metadata.internal/mcp",
    "file:///etc/passwd", "https://localhost/mcp"]) {
    await assert.rejects(client.preflight(url));
  }
  assert.equal(s.calls.length, 0);
  assert.equal(publicEndpoint("https://EXAMPLE.com:443/mcp"), "https://example.com/mcp");
});

test("public address screening includes special IPv4 and IPv6 ranges", () => {
  for (const ip of ["100.64.0.1", "192.0.2.1", "198.51.100.1", "203.0.113.1", "169.254.169.254",
    "224.1.1.1", "::1", "fd00::1", "fe80::1", "2001:db8::1", "2002:7f00:1::1", "::ffff:8.8.8.8"]) {
    assert.equal(publicAddress(ip), false, ip);
  }
  for (const ip of ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111", "2001:4860:4860::8888"]) assert.ok(publicAddress(ip), ip);
});

test("mixed public/private DNS results prevent any HTTP disclosure", async () => {
  const s = setup({ resolve: async () => [{ address: "93.184.216.34" }, { address: "10.0.0.1" }] });
  const { client } = await s.ready;
  await assert.rejects(client.preflight("https://example.com/mcp"), /public addresses/);
  assert.equal(s.calls.length, 0);
});

test("project-disabled or stdio servers replace same-named global entries", async () => {
  const s = setup({ env: { AGENT_GUILD_SCAN_MCP: "1" },
    readFile: async (path: string) => {
      if (path === "/h/.pi/agent/mcp.json") return JSON.stringify({ mcpServers: {
        disabled: { url: "https://disabled.example/mcp" }, replaced: { url: "https://replaced.example/mcp" } } });
      if (path === "/project/.pi/mcp.json") return JSON.stringify({ mcpServers: {
        disabled: { disabled: true }, replaced: { command: "local-tool" }, active: { url: "https://active.example/mcp" } } });
      throw new Error("ENOENT");
    } });
  await s.ready;
  for (const handler of s.hooks.get("session_start")!) await handler({}, s.ctx);
  assert.deepEqual(s.calls.map(u => u.searchParams.get("url")), ["https://active.example/mcp"]);
});

test("cached observations expire and are cleared at session boundaries", async () => {
  let now = 1000;
  const s = setup({ now: () => now }); const { client } = await s.ready;
  const url = "https://example.com/mcp";
  await client.preflight(url); assert.ok(client.verdictFor(url));
  assert.equal(client.verdictFor("https://example.com/another"), undefined);
  now += 300000; assert.equal(client.verdictFor(url), undefined);
  await client.preflight(url);
  for (const handler of s.hooks.get("session_shutdown")!) await handler({}, s.ctx);
  assert.equal(client.verdictFor(url), undefined);
  await client.preflight(url);
  for (const handler of s.hooks.get("session_start")!) await handler({}, s.ctx);
  assert.equal(client.verdictFor(url), undefined);
});

test("an in-flight response cannot repopulate a cleared session", async () => {
  let release!: () => void;
  const wait = new Promise<void>(resolve => { release = resolve; });
  const s = setup({ fetch: async (url: URL) => { await wait; return Response.json({
    target: url.searchParams.get("url"), verdict: "do_not_delegate",
    checks: [{ check: "protocol_handshake", status: "failed" }] }); } });
  const { client } = await s.ready;
  const pending = client.preflight("https://example.com/mcp");
  client.clear(); release(); await pending;
  assert.equal(client.verdictFor("https://example.com/mcp"), undefined);
});

test("malformed or wrong-endpoint observations are errors, not cacheable verdicts", async () => {
  for (const body of [null, {}, { target: "https://other.example/mcp", verdict: "no_failed_checks", checks: [] }]) {
    const s = setup({ fetch: async () => Response.json(body) }); const { client } = await s.ready;
    await assert.rejects(client.preflight("https://example.com/mcp"));
    assert.equal(client.verdictFor("https://example.com/mcp"), undefined);
  }
});

test("large service responses and pre-aborted requests stop without a verdict", async () => {
  const s = setup({ fetch: async () => new Response("x".repeat(65000)) }); const { client } = await s.ready;
  await assert.rejects(client.preflight("https://example.com/mcp"), /64 KB/);
  const abort = new AbortController(); abort.abort(new Error("cancelled"));
  await assert.rejects(client.preflight("https://example.com/mcp", abort.signal), /cancelled/);
});

test("quotes preserve small USDC amounts and never assume an unknown asset is dollars", () => {
  const usdc = GuildClient.describeChallenge({ accepts: [{ network: "eip155:8453",
    asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", amount: "1" }] });
  assert.match(usdc, /\$0\.000001 USD Coin/);
  const other = GuildClient.describeChallenge({ accepts: [{ network: "eip155:8453",
    asset: "unknown-token", amount: "10000", extra: { name: "USD Coin" } }] });
  assert.match(other, /10000 raw units of unknown-token/);
  assert.doesNotMatch(other, /\$|10000 USD Coin/);
});

test("a stalled DNS lookup has a bounded deadline and makes no HTTP request", async () => {
  const s = setup({ resolve: () => new Promise(() => {}),
    readFile: async () => JSON.stringify({ timeoutMs: 1000 }) });
  const { client } = await s.ready;
  await assert.rejects(client.preflight("https://example.com/mcp"), /DNS lookup timed out/);
  assert.equal(s.calls.length, 0);
});
