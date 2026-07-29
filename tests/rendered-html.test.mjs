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
  assert.match(html, /trust purchase/i);
  assert.match(html, /&quot;amount&quot;: 0/);
});

test("publishes an A2A agent card", async () => {
  const response = await render("/.well-known/agent-card.json");
  assert.equal(response.status, 200);
  const card = await response.json();
  assert.equal(card.protocolVersion, "0.3.0");
  assert.equal(card.version, "1.1.0");
  assert.equal(card.url, "http://localhost/a2a");
  assert.equal(card.agentGuild.agent_id, "agent_c7d2e902dc50");
  assert.equal(card.agentGuild.commerce.paid_action.price_usd, 1);
  assert.equal(
    card.agentGuild.commerce.public_tools[0].endpoint,
    "http://localhost/api/payan-readiness",
  );
  assert.equal(
    card.agentGuild.commerce.paid_action.network,
    "eip155:8453",
  );
  assert.deepEqual(
    card.skills.map((skill) => skill.id),
    [
      "fact-check",
      "code-review",
      "research",
      "coding",
      "web-research",
      "code_review",
    ],
  );
});

test("publishes legacy, commerce, and LLM discovery surfaces", async () => {
  const [legacy, commerce, llms, robots, sitemap] = await Promise.all([
    render("/.well-known/agent.json"),
    render("/commerce.json"),
    render("/llms.txt"),
    render("/robots.txt"),
    render("/sitemap.xml"),
  ]);

  assert.equal(legacy.status, 200);
  const legacyCard = await legacy.json();
  assert.equal(legacyCard.agentGuild.agent_id, "agent_c7d2e902dc50");
  assert.equal(legacyCard.version, "1.1.0");

  assert.equal(commerce.status, 200);
  const catalog = await commerce.json();
  assert.equal(catalog.paid_action.protocol, "x402-v2");
  assert.equal(catalog.paid_action.price.amount, 1);
  assert.equal(catalog.work_intake.template.amount, 0);
  assert.equal(
    catalog.public_tools[0].endpoint,
    "http://localhost/api/payan-readiness",
  );
  assert.match(
    catalog.paid_action.endpoints["fact-check"],
    /\/check\?capability=fact-check&signed=true&ttl_seconds=3600$/,
  );
  assert.match(
    catalog.paid_action.endpoints.coding,
    /\/check\?capability=coding&signed=true&ttl_seconds=3600$/,
  );
  assert.match(
    catalog.paid_action.endpoints["web-research"],
    /\/check\?capability=web-research&signed=true&ttl_seconds=3600$/,
  );

  assert.equal(llms.status, 200);
  assert.match(llms.headers.get("content-type") ?? "", /^text\/plain\b/i);
  const llmsText = await llms.text();
  assert.match(llmsText, /Agent Guild worker/);
  assert.match(llmsText, /POST https:\/\/agent-guild-5d5r\.onrender\.com\/offers/);
  assert.match(llmsText, /\$1\.00 USDC/);
  assert.match(llmsText, /offline-verifiable/);
  assert.match(llmsText, /signed=true&ttl_seconds=3600/);
  assert.match(llmsText, /PAYMENT-RESPONSE/);
  assert.match(llmsText, /agent_c7d2e902dc50/);
  assert.match(llmsText, /coding:/);
  assert.match(llmsText, /web-research:/);
  assert.match(llmsText, /POST http:\/\/localhost\/api\/payan-readiness/);

  assert.equal(robots.status, 200);
  assert.match(await robots.text(), /Sitemap: http:\/\/localhost\/sitemap\.xml/);

  assert.equal(sitemap.status, 200);
  assert.match(sitemap.headers.get("content-type") ?? "", /^application\/xml\b/i);
  assert.match(await sitemap.text(), /\/\.well-known\/agent-card\.json/);
  assert.match(await (await render("/sitemap.xml")).text(), /\/commerce\.json/);
});

test("publishes a bounded PayanAgent readiness utility", async () => {
  const description = await render("/api/payan-readiness");
  assert.equal(description.status, 200);
  const service = await description.json();
  assert.equal(service.service, "PayanAgent x402 offer readiness");
  assert.equal(service.method, "POST");
  assert.match(service.endpoint, /\/api\/payan-readiness$/);
  assert.match(service.safety, /No payment is signed or sent/);

  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("readiness", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const invalid = await worker.fetch(
    new Request("http://localhost/api/payan-readiness", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ offerId: "not-a-payan-offer" }),
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

  assert.equal(invalid.status, 400);
  assert.equal((await invalid.json()).error, "invalid_offer_id");
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
  assert.equal(message.paid_action.protocol, "x402-v2");
  assert.equal(message.paid_action.price.amount, 1);
  assert.equal(message.next_action.body.amount, 0);
  assert.match(
    message.next_action.body.terms.guild_vetting_payment.resource,
    /capability=fact-check&signed=true&ttl_seconds=3600$/,
  );
  assert.equal(message.next_action.body.worker_id, "agent_c7d2e902dc50");
  assert.match(message.next_action.call, /\/offers$/);
});

test("routes exact unmet-demand aliases into the signed-offer flow", async () => {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("alias", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const response = await worker.fetch(
    new Request("http://localhost/a2a", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: "alias-1",
        method: "message/send",
        params: {
          message: {
            role: "user",
            messageId: "test-alias-1",
            parts: [{ kind: "text", text: "coding offer" }],
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
  assert.equal(message.requested_capability, "coding");
  assert.equal(message.next_action.body.capability, "coding");
  assert.match(
    message.paid_action.call,
    /\/check\?capability=coding&signed=true&ttl_seconds=3600$/,
  );
});
