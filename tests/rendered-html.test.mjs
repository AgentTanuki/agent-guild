import assert from "node:assert/strict";
import test from "node:test";

async function render(path = "/") {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request(`http://localhost${path}`, {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the worker identity and honest revenue state", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = (await response.text()).replaceAll("<!-- -->", "");
  assert.match(html, /Codex Autonomous Worker/);
  assert.match(html, /agent_c7d2e902dc50/);
  assert.match(html, /EXTERNAL REVENUE/);
  assert.match(html, /\$0\.00/);
  assert.match(html, /Machine work/);
  assert.match(html, /signed offer/i);
});

test("publishes an A2A agent card", async () => {
  const response = await render("/.well-known/agent-card.json");
  assert.equal(response.status, 200);
  const card = await response.json();
  assert.equal(card.protocolVersion, "0.3.0");
  assert.equal(card.url, "http://localhost/a2a");
  assert.equal(card.agentGuild.agent_id, "agent_c7d2e902dc50");
  assert.deepEqual(
    card.skills.map((skill) => skill.id),
    ["fact-check", "code-review", "research"],
  );
});

test("answers A2A message/send with signed-offer instructions", async () => {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("a2a", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const response = await worker.fetch(
    new Request("http://localhost/a2a", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: 1,
        method: "message/send",
        params: {
          message: {
            role: "user",
            messageId: "test-1",
            parts: [{ kind: "text", text: "fact-check offer" }],
          },
        },
      }),
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );

  assert.equal(response.status, 200);
  const payload = await response.json();
  const message = JSON.parse(payload.result.parts[0].text);
  assert.equal(message.kind, "signed_offer_intake");
  assert.equal(message.requested_capability, "fact-check");
  assert.equal(message.next_action.body.worker_id, "agent_c7d2e902dc50");
  assert.match(message.next_action.call, /\/offers$/);
});
