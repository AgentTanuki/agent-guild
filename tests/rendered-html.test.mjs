import assert from "node:assert/strict";
import { generateKeyPairSync, verify } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

function canonicalize(value) {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") return JSON.stringify(value);
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalize(item)).join(",")}]`;
  }
  const keys = Object.keys(value).sort();
  return `{${keys
    .map((key) => `${JSON.stringify(key)}:${canonicalize(value[key])}`)
    .join(",")}}`;
}

async function renderRequest(path = "/", init = {}) {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request(`http://localhost${path}`, init),
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

async function render(path = "/") {
  return renderRequest(path, { headers: { accept: "text/html" } });
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
  assert.match(html, /Use one-call envelope SDK/);
  assert.match(html, /trust purchase/i);
  assert.match(html, /&quot;amount&quot;: 0/);
});

test("keeps CDN-served machine discovery aligned with the worker catalog", async () => {
  const staticLlms = await readFile(
    new URL("../public/llms.txt", import.meta.url),
    "utf8",
  );
  const staticSitemap = await readFile(
    new URL("../public/sitemap.xml", import.meta.url),
    "utf8",
  );
  assert.match(staticLlms, /POST https:\/\/codex-autonomous-worker\.rwdburley\.chatgpt\.site\/envelopes\/issue/);
  assert.match(staticLlms, /\$0\.01 USDC/);
  assert.match(staticLlms, /payload bytes never leave the caller/i);
  assert.match(staticLlms, /agentguild_envelope_client\.mjs/);
  assert.match(staticLlms, /createEvmMachineEnvelopeClient/);
  assert.match(staticSitemap, /\/openapi\.json/);
});

test("publishes an A2A agent card", async () => {
  const response = await render("/.well-known/agent-card.json");
  assert.equal(response.status, 200);
  const card = await response.json();
  assert.equal(card.protocolVersion, "0.3.0");
  assert.equal(card.version, "1.4.0");
  assert.equal(card.url, "http://localhost/a2a");
  assert.equal(card.agentGuild.agent_id, "agent_c7d2e902dc50");
  assert.equal(card.agentGuild.commerce.paid_action.price_usd, 1);
  assert.equal(card.agentGuild.commerce.openapi, "http://localhost/openapi.json");
  assert.equal(
    card.agentGuild.commerce.machine_envelope.client,
    "https://agent-guild-5d5r.onrender.com/sdk/agentguild_envelope_client.mjs",
  );
  assert.equal(
    card.agentGuild.commerce.public_tools[0].endpoint,
    "http://localhost/api/signed-agent-guild-preflight",
  );
  assert.equal(
    card.agentGuild.commerce.public_tools[1].endpoint,
    "http://localhost/api/agent-guild-preflight",
  );
  assert.equal(
    card.agentGuild.commerce.public_tools[2].endpoint,
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
      "agent-guild-preflight",
      "signed-agent-guild-preflight",
      "agent-guild-machine-envelope",
    ],
  );
});

test("publishes legacy, commerce, OpenAPI, and LLM discovery surfaces", async () => {
  const [legacy, commerce, openapi, llms, robots, sitemap] = await Promise.all([
    render("/.well-known/agent.json"),
    render("/commerce.json"),
    render("/openapi.json"),
    render("/llms.txt"),
    render("/robots.txt"),
    render("/sitemap.xml"),
  ]);

  assert.equal(legacy.status, 200);
  const legacyCard = await legacy.json();
  assert.equal(legacyCard.agentGuild.agent_id, "agent_c7d2e902dc50");
  assert.equal(legacyCard.version, "1.4.0");

  assert.equal(commerce.status, 200);
  const catalog = await commerce.json();
  assert.equal(catalog.paid_action.protocol, "x402-v2");
  assert.equal(catalog.paid_action.price.amount, 1);
  assert.equal(catalog.work_intake.template.amount, 0);
  assert.equal(catalog.machine_openapi.document, "http://localhost/openapi.json");
  assert.equal(
    catalog.machine_openapi.canonical_server,
    "https://agent-guild-5d5r.onrender.com",
  );
  assert.deepEqual(catalog.machine_openapi.operations, [
    "deep_preflight",
    "evidence_bundle",
    "machine_envelope",
  ]);
  assert.equal(
    catalog.machine_openapi.machine_envelope_client,
    "https://agent-guild-5d5r.onrender.com/sdk/agentguild_envelope_client.mjs",
  );
  assert.equal(
    catalog.public_tools[0].endpoint,
    "http://localhost/api/signed-agent-guild-preflight",
  );
  assert.match(catalog.public_tools[0].free_alternative, /\/preflight\?url=/);
  assert.match(catalog.public_tools[0].issuer_boundary, /worker-signed/);
  assert.equal(
    catalog.public_tools[1].endpoint,
    "http://localhost/api/agent-guild-preflight",
  );
  assert.equal(
    catalog.public_tools[2].endpoint,
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

  assert.equal(openapi.status, 200);
  assert.equal(openapi.headers.get("access-control-allow-origin"), "*");
  const spec = await openapi.json();
  assert.equal(spec.openapi, "3.1.0");
  assert.equal(spec.servers[0].url, "http://localhost");
  assert.equal(spec.servers[1].url, "https://agent-guild-5d5r.onrender.com");
  assert.equal(
    spec.paths["/preflight/deep"].get.responses["402"].headers["PAYMENT-REQUIRED"].schema.type,
    "string",
  );
  assert.equal(
    spec.paths["/preflight/deep"].get["x-payment-info"].priceUsd,
    0.02,
  );
  assert.equal(
    spec.paths["/evidence/bundle"].post["x-payment-info"].priceUsd,
    0.1,
  );
  assert.equal(
    spec.paths["/evidence/bundle"].post.requestBody.content["application/json"].example.url,
    "https://codex-autonomous-worker.rwdburley.chatgpt.site/a2a",
  );
  assert.equal(
    spec.paths["/envelopes/issue"].post["x-payment-info"].priceUsd,
    0.01,
  );
  assert.equal(
    spec.paths["/envelopes/issue"].post["x-client-sdk"].source,
    "https://agent-guild-5d5r.onrender.com/sdk/agentguild_envelope_client.mjs",
  );
  assert.equal(
    spec.paths["/envelopes/issue"].post.parameters[0].name,
    "X-Guild-Caller-Proof",
  );
  assert.equal(
    spec.paths["/envelopes/issue"].post.requestBody.content["application/json"].schema.$ref,
    "#/components/schemas/MachineEnvelopeRequest",
  );
  assert.match(
    spec["x-discovery-bridge"].settlementBoundary,
    /settle directly with Agent Guild/,
  );
  assert.equal(spec["x-discovery-bridge"].custody, "none");
  assert.equal(spec.paths["/evidence/verify"], undefined);

  const wellKnownOpenApi = await render(
    "/.well-known/agent-guild-commerce-openapi.json",
  );
  assert.equal(wellKnownOpenApi.status, 200);
  assert.equal((await wellKnownOpenApi.json()).info.title, spec.info.title);

  const deepAlias = await render(
    "/preflight/deep?url=https%3A%2F%2Fpublic-agent.example%2Fa2a",
  );
  assert.equal(deepAlias.status, 307);
  assert.equal(
    deepAlias.headers.get("location"),
    "https://agent-guild-5d5r.onrender.com/preflight/deep?url=https%3A%2F%2Fpublic-agent.example%2Fa2a",
  );

  const evidenceAlias = await renderRequest(
    "/evidence/bundle",
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        url: "https://public-agent.example/a2a",
        ttl_seconds: 3600,
      }),
    },
  );
  assert.equal(evidenceAlias.status, 307);
  assert.equal(
    evidenceAlias.headers.get("location"),
    "https://agent-guild-5d5r.onrender.com/evidence/bundle",
  );

  const envelopeAlias = await renderRequest(
    "/envelopes/issue",
    {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-guild-caller-proof": "proof-preserved-by-the-307-client",
      },
      body: JSON.stringify({
        kind: "intent",
        recipient: "did:key:recipient",
        payload_sha256:
          "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
        nonce: "buyer-unique-message-0001",
      }),
    },
  );
  assert.equal(envelopeAlias.status, 307);
  assert.equal(
    envelopeAlias.headers.get("location"),
    "https://agent-guild-5d5r.onrender.com/envelopes/issue",
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
  assert.match(llmsText, /POST http:\/\/localhost\/api\/agent-guild-preflight/);
  assert.match(llmsText, /POST http:\/\/localhost\/api\/signed-agent-guild-preflight/);
  assert.match(llmsText, /\/\.well-known\/worker-signing-key\.json/);
  assert.match(llmsText, /issuer is Codex-Autonomous-Worker, not Agent Guild/i);
  assert.match(llmsText, /upstream call is free/i);
  assert.match(llmsText, /machine-commerce OpenAPI: http:\/\/localhost\/openapi\.json/);
  assert.match(llmsText, /canonical calls and settlements remain/i);
  assert.match(llmsText, /POST http:\/\/localhost\/envelopes\/issue/);
  assert.match(llmsText, /payload bytes never leave the caller/i);
  assert.match(llmsText, /agentguild_envelope_client\.mjs/);
  assert.match(llmsText, /createEvmMachineEnvelopeClient/);

  assert.equal(robots.status, 200);
  assert.match(await robots.text(), /Sitemap: http:\/\/localhost\/sitemap\.xml/);

  assert.equal(sitemap.status, 200);
  assert.match(sitemap.headers.get("content-type") ?? "", /^application\/xml\b/i);
  assert.match(await sitemap.text(), /\/\.well-known\/agent-card\.json/);
  assert.match(await (await render("/sitemap.xml")).text(), /worker-signing-key\.json/);
  assert.match(await (await render("/sitemap.xml")).text(), /\/commerce\.json/);
  assert.match(await (await render("/sitemap.xml")).text(), /\/openapi\.json/);
});

test("issues caller-bound signed preflight snapshots verifiable offline", async () => {
  const { publicKey, privateKey } = generateKeyPairSync("ed25519");
  const privateKeyBase64 = privateKey
    .export({ format: "der", type: "pkcs8" })
    .toString("base64");
  const publicJwk = publicKey.export({ format: "jwk" });
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("signed-preflight", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const originalFetch = globalThis.fetch;

  try {
    globalThis.fetch = async (input) => {
      const url = String(input);
      if (url.includes("/preflight?url=")) {
        return Response.json({
          verdict: "delegate_with_caution",
          target: "https://public-agent.example/a2a",
          checks: [{ id: "a2a", status: "pass" }],
        });
      }
      if (url.endsWith("/release")) {
        return Response.json({ version: "2.0.3", git_sha: "abc123" });
      }
      throw new Error(`unexpected upstream ${url}`);
    };

    const env = {
      ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
      WORKER_ED25519_PRIVATE_KEY_PKCS8_B64: privateKeyBase64,
      WORKER_ED25519_PUBLIC_JWK_JSON: JSON.stringify(publicJwk),
    };
    const response = await worker.fetch(
      new Request("http://localhost/api/signed-agent-guild-preflight", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          url: "https://public-agent.example/a2a",
          recipient: "did:key:buyer",
          nonce: "buyer-nonce-123",
          purpose: "pre-delegation endpoint trust",
        }),
      }),
      env,
      { waitUntil() {}, passThroughOnException() {} },
    );

    assert.equal(response.status, 200);
    const envelope = await response.json();
    assert.equal(
      envelope.payload.type,
      "agent-guild/caller-bound-preflight-snapshot/v1",
    );
    assert.equal(envelope.payload.recipient, "did:key:buyer");
    assert.equal(envelope.payload.nonce, "buyer-nonce-123");
    assert.equal(envelope.payload.issuer.boundary, "worker-signed; not Agent-Guild-signed");
    assert.equal(envelope.payload.agent_guild.release.git_sha, "abc123");
    assert.equal(envelope.payload.agent_guild.result.verdict, "delegate_with_caution");
    assert.equal(
      new Date(envelope.payload.expires_at).getTime() -
        new Date(envelope.payload.issued_at).getTime(),
      300_000,
    );

    const signature = Buffer.from(
      envelope.proof.signature_base64url,
      "base64url",
    );
    assert.equal(
      verify(
        null,
        Buffer.from(canonicalize(envelope.payload)),
        publicKey,
        signature,
      ),
      true,
    );
    const tampered = structuredClone(envelope.payload);
    tampered.subject.endpoint = "https://attacker.example/a2a";
    assert.equal(
      verify(null, Buffer.from(canonicalize(tampered)), publicKey, signature),
      false,
    );

    const keyResponse = await worker.fetch(
      new Request("http://localhost/.well-known/worker-signing-key.json"),
      env,
      { waitUntil() {}, passThroughOnException() {} },
    );
    const keyDocument = await keyResponse.json();
    assert.equal(keyDocument.configured, true);
    assert.equal(keyDocument.publicKeyJwk.x, publicJwk.x);
    assert.match(keyDocument.agent_guild_identity.note, /not an Agent Guild issuer key/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("publishes an honest Agent Guild preflight adapter", async () => {
  const description = await render("/api/agent-guild-preflight");
  assert.equal(description.status, 200);
  const service = await description.json();
  assert.equal(service.service, "Agent Guild endpoint preflight adapter");
  assert.equal(service.method, "POST");
  assert.match(service.endpoint, /\/api\/agent-guild-preflight$/);
  assert.match(service.freeAlternative, /\/preflight\?url=<url>$/);
  assert.match(service.commerce, /upstream check is free/i);
  assert.match(service.safety, /accepts no credentials/i);

  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("preflight", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const invalid = await worker.fetch(
    new Request("http://localhost/api/agent-guild-preflight", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ url: "file:///etc/passwd" }),
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
  assert.equal((await invalid.json()).error, "invalid_url");
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
