import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
const cliRequire = createRequire(
  new URL("../node_modules/@botpress/cli/package.json", import.meta.url),
);
const { build } = cliRequire("esbuild");

// Build only the definition loader. The action under test is the actual `bp build` bundle.
await build({
  entryPoints: ["integration.definition.ts"],
  bundle: true,
  packages: "external",
  platform: "node",
  format: "cjs",
  outfile: ".test/definition.cjs",
  logLevel: "silent",
});
const require = createRequire(import.meta.url);
const definition = require("../.test/definition.cjs").default;
const native = require("../.botpress/dist/index.cjs");
const ORIGIN = "https://agent-guild-5d5r.onrender.com";
const URL_VALUE = "https://worker.example.org/mcp?mode=one&label=a%20b";
const CHECKS = [
  "endpoint_reachable",
  "protocol_handshake",
  "agent_card_resolves",
  "agent_card_signed",
  "payment_claim_holds",
  "independent_evidence",
];
const action = definition.actions.observeEndpoint;
const metadata = {
  "x-bot-id": "fixture-bot",
  "x-bot-user-id": "fixture-user",
  "x-integration-id": "fixture-integration",
  "x-integration-alias": "agent-guild-observations",
  "x-webhook-id": "fixture-webhook",
  "x-bp-configuration": Buffer.from("{}").toString("base64"),
  "x-bp-operation": "action_triggered",
};

function report(
  statuses = ["proven", "proven", "proven", "unknown", "unknown", "unknown"],
  target = URL_VALUE,
) {
  const checks = CHECKS.map((check, i) => ({ check, status: statuses[i] }));
  const failed = checks
    .filter((x) => x.status === "failed")
    .map((x) => x.check);
  return {
    target,
    checks,
    failed,
    unknowns: checks.filter((x) => x.status === "unknown").map((x) => x.check),
    scored: checks.filter((x) => x.status !== "unknown").map((x) => x.check),
    verdict: failed.some((x) => CHECKS.slice(0, 2).includes(x))
      ? "do_not_delegate"
      : failed.length
        ? "delegate_with_caution"
        : "no_failed_checks",
  };
}
function jsonResponse(value, init = {}) {
  return new Response(
    typeof value === "string" ? value : JSON.stringify(value),
    {
      status: 200,
      headers: { "content-type": "application/json; charset=utf-8" },
      ...init,
    },
  );
}
async function invoke(
  t,
  {
    input = { url: URL_VALUE },
    value = report(),
    fetcher,
    raw,
    direct = false,
  } = {},
) {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (...args) => {
    calls.push(args);
    return fetcher ? fetcher(...args) : jsonResponse(raw ?? value);
  });
  let output;
  if (direct) {
    output = await native.default.actions.observeEndpoint({ input });
  } else {
    const response = await native.handler({
      headers: metadata,
      body: JSON.stringify({
        type: "observeEndpoint",
        input,
        conversation: "fixture private conversation must never be forwarded",
      }),
    });
    assert.equal(response.status, 200);
    const envelope = JSON.parse(response.body);
    assert.deepEqual(Object.keys(envelope).sort(), ["meta", "output"]);
    assert.deepEqual(envelope.meta, { cost: 0 });
    output = envelope.output;
  }
  assert.deepEqual(action.output.schema.parse(output), output);
  return { output, calls };
}
function failure(output, code, status = "unavailable") {
  assert.equal(output.status, status);
  assert.equal(output.code, code);
  assert.equal(output.verdict, null);
  for (const field of [
    "target",
    "requestedAt",
    "completedAt",
    "httpStatus",
    "responseBytes",
  ])
    assert.equal(output[field], null);
  for (const field of ["checks", "failed", "unknowns", "scored"])
    assert.deepEqual(output[field], []);
}

test("native definition, generated action types, and CLI bundle are present", () => {
  assert.deepEqual(Object.keys(definition.actions), ["observeEndpoint"]);
  assert.equal(definition.title, "Agent Guild Endpoint Observations");
  assert.equal(definition.name, "agent-guild-observations");
  assert.equal(typeof native.handler, "function");
  assert.equal(native.handler, native.default.handler);
  assert.deepEqual(Object.keys(native.default.actions), ["observeEndpoint"]);
  assert.deepEqual(definition.configuration.schema.parse({}), {});
  assert.throws(() =>
    definition.configuration.schema.parse({ key: "unwanted" }),
  );
  assert.deepEqual(action.input.schema.parse({ url: URL_VALUE }), {
    url: URL_VALUE,
  });
  for (const input of [
    null,
    [],
    {},
    { url: 1 },
    { url: URL_VALUE, extra: true },
  ])
    assert.throws(() => action.input.schema.parse(input));
  assert.match(
    readFileSync(
      ".botpress/implementation/typings/actions/observeEndpoint/output.ts",
      "utf8",
    ),
    /unknowns/,
  );
  assert.match(
    readFileSync(
      ".botpress/implementation/typings/actions/observeEndpoint/input.ts",
      "utf8",
    ),
    /url/,
  );
});

test("real SDK action dispatch sends one exact URL and only fixed headers; unknowns stay unknown", async (t) => {
  const { output, calls } = await invoke(t);
  assert.equal(output.status, "observed");
  assert.equal(output.code, null);
  assert.equal(output.target, URL_VALUE);
  assert.deepEqual(output.checks, report().checks);
  assert.deepEqual(output.unknowns, CHECKS.slice(3));
  assert.deepEqual(output.scored, CHECKS.slice(0, 3));
  assert.equal(output.verdict, "no_failed_checks");
  assert.equal(output.serviceOrigin, ORIGIN);
  assert.equal(output.httpStatus, 200);
  assert.equal(
    output.responseBytes,
    Buffer.byteLength(JSON.stringify(report())),
  );
  assert.ok(Date.parse(output.completedAt) >= Date.parse(output.requestedAt));
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0][0],
    ORIGIN + "/preflight?url=" + encodeURIComponent(URL_VALUE),
  );
  const options = calls[0][1];
  assert.equal(options.method, "GET");
  assert.deepEqual(options.headers, {
    Accept: "application/json",
    "User-Agent": "AgentGuild-Botpress-EndpointObservations/0.1.0",
  });
  assert.equal(options.redirect, "error");
  assert.equal(options.credentials, "omit");
  assert.equal(options.referrerPolicy, "no-referrer");
  assert.equal(options.body, undefined);
  assert.ok(options.signal instanceof AbortSignal);
  assert.ok(options.signal.aborted);
  assert.ok(output.limitations.some((x) => x.includes("Unknown")));
});

for (const [name, statuses, verdict] of [
  ["all unknown", Array(6).fill("unknown"), "no_failed_checks"],
  ["all proven", Array(6).fill("proven"), "no_failed_checks"],
  [
    "reachability failed",
    ["failed", "proven", "unknown", "unknown", "unknown", "unknown"],
    "do_not_delegate",
  ],
  [
    "handshake failed",
    ["proven", "failed", "proven", "unknown", "unknown", "unknown"],
    "do_not_delegate",
  ],
  [
    "other check failed",
    ["proven", "proven", "failed", "unknown", "unknown", "unknown"],
    "delegate_with_caution",
  ],
])
  test("consistent verdict: " + name, async (t) => {
    const { output } = await invoke(t, { value: report(statuses) });
    assert.equal(output.status, "observed");
    assert.equal(output.verdict, verdict);
    assert.deepEqual(output.unknowns, report(statuses).unknowns);
  });

test("public HTTP targets and exact lexical encoding remain supported", async (t) => {
  const url = "http://Worker.EXAMPLE.org:8080/MCP?x=%2f&y=%2F&z=é";
  const { output, calls } = await invoke(t, {
    input: { url },
    value: report(undefined, url),
  });
  assert.equal(output.target, url);
  assert.equal(
    calls[0][0],
    ORIGIN + "/preflight?url=" + encodeURIComponent(url),
  );
});

test("remote prose, instructions, proof, and links never enter output", async (t) => {
  const marker = "REMOTE_INSTRUCTION_NEVER_FORWARD";
  const value = report();
  Object.assign(value, {
    prose: marker,
    limitations: [marker],
    proof: marker,
    link: "https://remote.example.org/" + marker,
  });
  value.checks.forEach((x) =>
    Object.assign(x, { proof: marker, notes: marker }),
  );
  const { output } = await invoke(t, { value });
  assert.equal(output.status, "observed");
  assert.ok(!JSON.stringify(output).includes(marker));
  assert.deepEqual(output.checks, report().checks);
});

test("check and summary order is not semantically significant", async (t) => {
  const value = report();
  value.checks.reverse();
  value.unknowns.reverse();
  value.scored.reverse();
  const { output } = await invoke(t, { value });
  assert.equal(output.status, "observed");
  assert.deepEqual(output.checks, report().checks);
});

for (const [name, alter, code] of [
  ["different target", (v) => (v.target += "/"), "target_mismatch"],
  ["missing target", (v) => delete v.target, "target_mismatch"],
  ["missing check", (v) => v.checks.pop(), "incomplete_response"],
  ["extra check", (v) => v.checks.push(v.checks[0]), "incomplete_response"],
  ["duplicate check", (v) => (v.checks[1] = v.checks[0]), "invalid_response"],
  [
    "unknown check",
    (v) => (v.checks[0].check = "safe_to_hire"),
    "invalid_response",
  ],
  ["invalid status", (v) => (v.checks[0].status = "safe"), "invalid_response"],
  ["null check", (v) => (v.checks[0] = null), "invalid_response"],
  ["omitted failed array", (v) => delete v.failed, "inconsistent_response"],
  [
    "incorrect failed array",
    (v) => v.failed.push(CHECKS[0]),
    "inconsistent_response",
  ],
  ["incorrect unknowns", (v) => v.unknowns.pop(), "inconsistent_response"],
  [
    "duplicate unknowns",
    (v) => (v.unknowns[1] = v.unknowns[0]),
    "inconsistent_response",
  ],
  ["missing scored", (v) => delete v.scored, "inconsistent_response"],
  [
    "unknown marked scored",
    (v) => v.scored.push(CHECKS[3]),
    "inconsistent_response",
  ],
  [
    "duplicate scored",
    (v) => (v.scored[1] = v.scored[0]),
    "inconsistent_response",
  ],
  [
    "wrong verdict",
    (v) => (v.verdict = "do_not_delegate"),
    "inconsistent_response",
  ],
  [
    "blocking failure hidden by clean verdict",
    (v) => {
      v.checks[0].status = "failed";
      v.failed = [CHECKS[0]];
      v.verdict = "no_failed_checks";
    },
    "inconsistent_response",
  ],
])
  test("unusable report: " + name, async (t) => {
    const value = report();
    alter(value);
    const { output, calls } = await invoke(t, { value });
    failure(output, code);
    assert.equal(calls.length, 1);
  });

for (const input of [
  null,
  [],
  {},
  { url: 1 },
  { url: null },
  { url: URL_VALUE, token: "never sent" },
  "url",
])
  test("dispatcher input guard: " + JSON.stringify(input), async (t) => {
    const { output, calls } = await invoke(t, { input });
    failure(output, "invalid_input", "rejected");
    assert.equal(calls.length, 0);
  });

for (const [name, input] of [
  [
    "accessor",
    Object.defineProperty({}, "url", {
      enumerable: true,
      get() {
        throw new Error("must not read");
      },
    }),
  ],
  ["symbol field", { ...{ url: URL_VALUE }, [Symbol("secret")]: "never sent" }],
  [
    "hidden field",
    Object.defineProperty({ url: URL_VALUE }, "secret", {
      value: "never sent",
    }),
  ],
  [
    "class instance",
    new (class {
      url = URL_VALUE;
    })(),
  ],
])
  test(
    "direct native action guard without JSON normalization: " + name,
    async (t) => {
      const { output, calls } = await invoke(t, { input, direct: true });
      failure(output, "invalid_input", "rejected");
      assert.equal(calls.length, 0);
    },
  );

for (const url of [
  "",
  " https://example.org",
  "https://example.org/a b",
  "https://user:pass@example.org",
  "https://example.org/#fragment",
  "file:///etc/passwd",
  "ftp://example.org/a",
  "http://localhost:9000",
  "http://127.0.0.1",
  "http://2130706433",
  "http://0x7f000001",
  "http://[::1]",
  "https://worker.local",
  "https://worker.internal",
  "https://worker.test",
  "https://worker.invalid",
  "https://worker.onion",
  "https://worker.example",
  "https://single",
  "https://example.org./a",
  "https://example.org/\\x",
  "https://example.org/\u0000",
  "https://example.org/\ud800",
  "https://example.org/" + "x".repeat(2049),
])
  test("unsupported URL: " + JSON.stringify(url).slice(0, 100), async (t) => {
    const { output, calls } = await invoke(t, { input: { url } });
    failure(output, "invalid_endpoint", "rejected");
    assert.equal(calls.length, 0);
  });

for (const status of [301, 302, 400, 402, 404, 429, 500])
  test(
    "HTTP " + status + " has no retry, payment, or redirect followup",
    async (t) => {
      const { output, calls } = await invoke(t, {
        fetcher: () => jsonResponse({ secret: "not forwarded" }, { status }),
      });
      failure(output, "http_unavailable");
      assert.equal(calls.length, 1);
    },
  );

test("redirect flag is rejected", async (t) => {
  const response = jsonResponse(report());
  Object.defineProperty(response, "redirected", { value: true });
  failure(
    (await invoke(t, { fetcher: () => response })).output,
    "http_unavailable",
  );
});
test("different final service URL is rejected", async (t) => {
  const response = jsonResponse(report());
  Object.defineProperty(response, "url", {
    value: "https://other.example.org",
  });
  failure(
    (await invoke(t, { fetcher: () => response })).output,
    "unexpected_response_url",
  );
});
test("exact final service URL is accepted", async (t) => {
  const response = jsonResponse(report());
  Object.defineProperty(response, "url", {
    value: ORIGIN + "/preflight?url=" + encodeURIComponent(URL_VALUE),
  });
  assert.equal(
    (await invoke(t, { fetcher: () => response })).output.status,
    "observed",
  );
});
test("network exception produces fixed local code only", async (t) => {
  const { output, calls } = await invoke(t, {
    fetcher: () => {
      throw new Error("remote secret");
    },
  });
  failure(output, "request_unavailable");
  assert.equal(calls.length, 1);
  assert.ok(!JSON.stringify(output).includes("remote secret"));
});
test("missing response body is unavailable", async (t) =>
  failure(
    (
      await invoke(t, {
        fetcher: () =>
          new Response(null, {
            headers: { "content-type": "application/json" },
          }),
      })
    ).output,
    "invalid_response",
  ));
test("non-JSON media type is unavailable", async (t) =>
  failure(
    (
      await invoke(t, {
        fetcher: () =>
          jsonResponse(report(), { headers: { "content-type": "text/plain" } }),
      })
    ).output,
    "invalid_response",
  ));

for (const raw of [
  "not JSON",
  '{"target":1,"target":2}',
  '{"targ\\u0065t":1,"target":2}',
  '{"n":1e9999}',
  "[".repeat(26) + "]".repeat(26),
  '{"x":true} garbage',
])
  test("strict bounded JSON rejects " + raw.slice(0, 65), async (t) =>
    failure((await invoke(t, { raw })).output, "invalid_response"),
  );
test("invalid UTF-8 is unavailable", async (t) =>
  failure(
    (
      await invoke(t, {
        fetcher: () =>
          new Response(new Uint8Array([0xc3, 0x28]), {
            headers: { "content-type": "application/json" },
          }),
      })
    ).output,
    "request_unavailable",
  ));
test("exact 65536 decoded bytes accepted; metadata counts original whitespace", async (t) => {
  const raw = JSON.stringify(report()).padEnd(65536, " ");
  const { output } = await invoke(t, { raw });
  assert.equal(output.status, "observed");
  assert.equal(output.responseBytes, 65536);
});
test("65537 decoded bytes rejected even across chunks", async (t) => {
  const raw = Buffer.from(JSON.stringify(report()).padEnd(65537, " "));
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(raw.subarray(0, 32768));
      controller.enqueue(raw.subarray(32768));
      controller.close();
    },
  });
  failure(
    (
      await invoke(t, {
        fetcher: () =>
          new Response(body, {
            headers: {
              "content-type": "application/json",
              "content-length": "1",
            },
          }),
      })
    ).output,
    "response_too_large",
  );
});
test("deadline returns even if host fetch ignores abort; signal is cancelled", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let signal;
  const pending = invoke(t, {
    fetcher: (_url, options) => {
      signal = options.signal;
      return new Promise(() => {});
    },
  });
  await Promise.resolve();
  t.mock.timers.tick(15001);
  const { output, calls } = await pending;
  failure(output, "request_timeout");
  assert.equal(calls.length, 1);
  assert.equal(signal.aborted, true);
});
test("deadline covers response body stall; stream cancellation cannot delay return", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let cancelled = false;
  const body = new ReadableStream({
    pull() {
      return new Promise(() => {});
    },
    cancel() {
      cancelled = true;
      return new Promise(() => {});
    },
  });
  const pending = invoke(t, {
    fetcher: () =>
      new Response(body, { headers: { "content-type": "application/json" } }),
  });
  for (let i = 0; i < 10; i++) await Promise.resolve();
  t.mock.timers.tick(15001);
  failure((await pending).output, "request_timeout");
  assert.equal(cancelled, true);
});
for (const operation of ["register", "unregister", "ping"])
  test(
    "native lifecycle " + operation + " has no external request",
    async (t) => {
      let calls = 0;
      t.mock.method(globalThis, "fetch", async () => {
        calls++;
        throw new Error("unexpected network");
      });
      const response = await native.handler({
        headers: { ...metadata, "x-bp-operation": operation },
        body: JSON.stringify({ webhookUrl: "https://fixture.example.org" }),
      });
      assert.equal(response.status, 200);
      assert.equal(calls, 0);
    },
  );
for (const config of [{ unexpected: true }, [], null])
  test(
    "native register rejects unsupported configuration " +
      JSON.stringify(config),
    async (t) => {
      let calls = 0;
      t.mock.method(globalThis, "fetch", async () => {
        calls++;
        throw new Error("unexpected network");
      });
      const response = await native.handler({
        headers: {
          ...metadata,
          "x-bp-operation": "register",
          "x-bp-configuration": Buffer.from(JSON.stringify(config)).toString(
            "base64",
          ),
        },
        body: "{}",
      });
      assert.notEqual(response.status, 200);
      assert.equal(calls, 0);
      assert.match(response.body, /no configuration fields/);
    },
  );
