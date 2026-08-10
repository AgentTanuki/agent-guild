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

function encodeAbiString(value) {
  const data = Buffer.from(value, "utf8").toString("hex");
  const padded = data.padEnd(Math.ceil(data.length / 64) * 64, "0");
  return `0x${"20".padStart(64, "0")}${Buffer.byteLength(value, "utf8")
    .toString(16)
    .padStart(64, "0")}${padded}`;
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
  assert.match(html, /Protect x402 payments/);
  assert.match(html, /trust purchase/i);
  assert.match(html, /&quot;amount&quot;: 0/);
});

test("publishes an indexable fail-closed agent spend policy", async () => {
  const response = await render("/agent-spend-policy");
  assert.equal(response.status, 200);
  const html = (await response.text()).replaceAll("<!-- -->", "");

  assert.match(html, /Agent Spend Policy for x402/);
  assert.match(html, /Signed authority/);
  assert.match(html, /before a machine spends/i);
  assert.match(html, /AGPD-1/);
  assert.match(html, /createAgentGuildX402PaymentPolicy/);
  assert.match(html, /client\.onBeforePaymentCreation/);
  assert.match(html, /\$0\.01/);
  assert.match(html, /Base/);
  assert.match(html, /No.*custody/i);
  assert.match(html, /It does not prove/i);
  assert.match(html, /application\/ld\+json/);
  assert.match(html, /Agent Guild x402 Agent Spend Policy/);
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
  assert.match(staticLlms, /GET https:\/\/codex-autonomous-worker\.rwdburley\.chatgpt\.site\/trust-decision/);
  assert.match(staticLlms, /GET https:\/\/codex-autonomous-worker\.rwdburley\.chatgpt\.site\/erc8004\/preflight\?agent_id=1/);
  assert.match(staticLlms, /transparent x402 relay/i);
  assert.match(staticLlms, /\$0\.01 USDC/);
  assert.match(staticLlms, /payload bytes and private keys never leave the caller/i);
  assert.match(staticLlms, /agentguild_envelope_client\.mjs/);
  assert.match(staticLlms, /createEvmMachineEnvelopeClient/);
  assert.match(staticLlms, /createAgentGuildX402PaymentPolicy/);
  assert.match(staticLlms, /client\.onBeforePaymentCreation\(policy\)/);
  assert.match(staticLlms, /agent-spend-policy/);
  assert.match(staticLlms, /autonomous wallet pre-authorization/i);
  assert.match(staticSitemap, /\/agent-spend-policy/);
  assert.match(staticSitemap, /\/openapi\.json/);
});

test("publishes an A2A agent card", async () => {
  const response = await render("/.well-known/agent-card.json");
  assert.equal(response.status, 200);
  const card = await response.json();
  assert.equal(card.protocolVersion, "0.3.0");
  assert.equal(card.version, "1.7.0");
  assert.equal(card.url, "http://localhost/a2a");
  assert.equal(card.agentGuild.agent_id, "agent_c7d2e902dc50");
  assert.equal(card.agentGuild.machine_catalog, "http://localhost/commerce.json");
  assert.equal(card.agentGuild.machine_openapi, "http://localhost/openapi.json");
  assert.equal(
    card.agentGuild.signing_key,
    "http://localhost/.well-known/worker-signing-key.json",
  );
  assert.equal(card.agentGuild.commerce, undefined);
  const cardBlob = JSON.stringify(card).toLowerCase();
  for (const marker of ["x402", "payment", "paid", "price", "usdc", "402"]) {
    assert.equal(cardBlob.includes(marker), false, `free A2A card contains ${marker}`);
  }
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
      "erc8004-agent-guild-preflight",
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
  assert.equal(legacyCard.version, "1.7.0");

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
  assert.equal(
    catalog.machine_openapi.x402_manifest,
    "http://localhost/.well-known/x402",
  );
  assert.deepEqual(catalog.machine_openapi.operations, [
    "worker_trust_decision",
    "deep_preflight",
    "erc8004_identity_preflight",
    "evidence_bundle",
    "machine_envelope",
    "payment_decision",
  ]);
  assert.equal(
    catalog.machine_openapi.machine_envelope_client,
    "https://agent-guild-5d5r.onrender.com/sdk/agentguild_envelope_client.mjs",
  );
  assert.equal(
    catalog.payment_policy.client.factory,
    "createAgentGuildX402PaymentPolicy({meteredFetch})",
  );
  assert.equal(catalog.payment_policy.price.amount, 0.01);
  assert.equal(catalog.payment_policy.contract, "AGPD-1/1.0");
  assert.equal(
    catalog.payment_policy.relay_endpoint,
    "http://localhost/wallet-binding/decision",
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
  assert.equal(
    catalog.paid_action.endpoints["fact-check"],
    "http://localhost/trust-decision?capability=fact-check",
  );
  assert.equal(
    catalog.paid_action.endpoints.coding,
    "http://localhost/trust-decision?capability=coding",
  );
  assert.equal(
    catalog.paid_action.endpoints["web-research"],
    "http://localhost/trust-decision?capability=web-research",
  );
  assert.match(
    catalog.paid_action.canonical_endpoints["fact-check"],
    /\/check\?capability=fact-check&signed=true&ttl_seconds=3600$/,
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
  assert.deepEqual(
    spec.paths["/preflight/deep"].get["x-payment-info"].protocols,
    [{ x402: {} }],
  );
  assert.deepEqual(
    spec.paths["/preflight/deep"].get["x-payment-info"].price,
    { mode: "fixed", currency: "USD", amount: "0.020000" },
  );
  assert.equal(
    spec.paths["/evidence/bundle"].post["x-payment-info"].priceUsd,
    0.1,
  );
  assert.equal(spec.info.version, "2.5.0");
  assert.match(spec.info["x-guidance"], /Base ERC-8004 agent_id/);
  assert.match(spec.info["x-guidance"], /POST \/envelopes\/issue/);
  assert.match(
    spec.paths["/preflight/deep"].get.summary,
    /before paying or delegating/,
  );
  assert.equal(
    spec.paths["/erc8004/preflight"].get["x-payment-info"].priceUsd,
    0.02,
  );
  assert.match(
    spec.paths["/erc8004/preflight"].get.summary,
    /Base ERC-8004 agent identity/,
  );
  assert.match(
    spec.paths["/wallet-binding/decision"].post.summary,
    /before the wallet signs/,
  );
  assert.match(
    spec.paths["/envelopes/issue"].post.summary,
    /sender, recipient, nonce, and expiry/,
  );
  assert.equal(
    spec.paths["/trust-decision"].get["x-payment-info"].priceUsd,
    1,
  );
  assert.deepEqual(
    spec.paths["/trust-decision"].get.parameters[0].schema.enum,
    ["fact-check", "code-review", "research", "coding", "web-research", "code_review"],
  );
  assert.equal(
    spec.paths["/envelopes/issue"].post["x-client-sdk"].factory,
    "createEvmMachineEnvelopeClient({evmSigner})",
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
    spec.paths["/wallet-binding/decision"].post["x-client-sdk"].factory,
    "createAgentGuildX402PaymentPolicy({meteredFetch})",
  );
  assert.equal(
    spec.paths["/wallet-binding/decision"].post["x-payment-info"].priceUsd,
    0.01,
  );
  assert.equal(spec.paths["/wallet-binding/decision"].post.servers, undefined);
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
  assert.match(llmsText, /x402 discovery manifest: http:\/\/localhost\/\.well-known\/x402/);
  assert.match(llmsText, /canonical calls and settlements remain/i);
  assert.match(llmsText, /transparent x402 relay/i);
  assert.match(llmsText, /GET http:\/\/localhost\/trust-decision/);
  assert.match(llmsText, /GET http:\/\/localhost\/erc8004\/preflight\?agent_id=1/);
  assert.match(llmsText, /POST http:\/\/localhost\/envelopes\/issue/);
  assert.match(llmsText, /payload bytes and private keys never leave the caller/i);
  assert.match(llmsText, /agentguild_envelope_client\.mjs/);
  assert.match(llmsText, /createEvmMachineEnvelopeClient/);
  assert.match(llmsText, /createAgentGuildX402PaymentPolicy/);
  assert.match(llmsText, /AGPD-1/);

  assert.equal(robots.status, 200);
  assert.match(await robots.text(), /Sitemap: http:\/\/localhost\/sitemap\.xml/);

  assert.equal(sitemap.status, 200);
  assert.match(sitemap.headers.get("content-type") ?? "", /^application\/xml\b/i);
  assert.match(await sitemap.text(), /\/\.well-known\/agent-card\.json/);
  assert.match(await (await render("/sitemap.xml")).text(), /worker-signing-key\.json/);
  assert.match(await (await render("/sitemap.xml")).text(), /\/commerce\.json/);
  assert.match(await (await render("/sitemap.xml")).text(), /\/openapi\.json/);
  assert.match(await (await render("/sitemap.xml")).text(), /\/\.well-known\/x402/);
});

test("publishes live x402 discovery with canonical terms and relay URLs", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async (input) => {
      const resource = input instanceof Request ? input.url : String(input);
      const capability = new URL(resource).searchParams.get("capability");
      const paymentRequired = {
        x402Version: 2,
        resource: {
          url: resource,
          description: `Signed decision for ${capability}`,
          mimeType: "application/json",
        },
        accepts: [
          {
            scheme: "exact",
            network: "eip155:8453",
            asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            amount: "1000000",
            payTo: "0xaa4E3ba0Eb5f564cAb54dDC08f5BaAfb3D4cA8E5",
          },
        ],
      };
      return new Response(null, {
        status: 402,
        headers: {
          "PAYMENT-REQUIRED": Buffer.from(
            JSON.stringify(paymentRequired),
          ).toString("base64"),
        },
      });
    };

    const response = await renderRequest("/.well-known/x402");
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("access-control-allow-origin"), "*");
    const manifest = await response.json();
    assert.equal(manifest.x402Version, 2);
    assert.equal(manifest.count, 3);
    assert.deepEqual(manifest.unavailableCapabilities, []);
    assert.deepEqual(
      manifest.resources.map(({ metadata }) => metadata.input.capability),
      ["fact-check", "code-review", "research"],
    );
    assert.equal(manifest.resources[0].accepts[0].amount, "1000000");
    assert.match(
      manifest.resources[0].resource,
      /agent-guild-5d5r\.onrender\.com\/check\?capability=fact-check/,
    );
    assert.equal(
      manifest.resources[0].metadata.relay,
      "http://localhost/trust-decision?capability=fact-check",
    );
    assert.match(
      manifest.resources[0].metadata.settlementBoundary,
      /Settlement and issuance terminate at Agent Guild/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("relays canonical x402 challenges and receipts without API keys or custody", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  const challengeDocument = {
    x402Version: 2,
    resource: {
      url: "https://agent-guild-5d5r.onrender.com/check?capability=fact-check",
      description: "Signed fact-check decision",
      mimeType: "application/json",
    },
    accepts: [{ scheme: "exact", network: "eip155:8453" }],
  };
  const encodedChallenge = Buffer.from(
    JSON.stringify(challengeDocument),
  ).toString("base64");
  const envelopeBody = JSON.stringify({
    kind: "intent",
    recipient: "did:key:recipient",
    payload_sha256:
      "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
    nonce: "buyer-unique-message-0001",
  });

  try {
    globalThis.fetch = async (input, init = {}) => {
      const requestUrl = input instanceof Request ? input.url : String(input);
      const requestHeaders = new Headers(init.headers);
      const requestBody =
        init.body instanceof ArrayBuffer
          ? new TextDecoder().decode(init.body)
          : init.body == null
            ? null
            : String(init.body);
      calls.push({ requestUrl, init, requestHeaders, requestBody });

      if (requestUrl.endsWith("/envelopes/issue")) {
        return new Response(JSON.stringify({ issued: true }), {
          status: 200,
          headers: {
            "content-type": "application/json",
            "PAYMENT-RESPONSE": "base-mainnet-receipt",
          },
        });
      }
      return new Response(null, {
        status: 402,
        headers: {
          "PAYMENT-REQUIRED": encodedChallenge,
        },
      });
    };

    const trustDecision = await renderRequest(
      "/trust-decision?capability=fact-check",
      {
        headers: {
          accept: "application/json",
          "PAYMENT-SIGNATURE": "buyer-payment-signature",
          "X-API-Key": "sandbox-credential-must-not-cross-relay",
        },
      },
    );
    assert.equal(trustDecision.status, 402);
    assert.equal(
      trustDecision.headers.get("PAYMENT-REQUIRED"),
      encodedChallenge,
    );
    assert.deepEqual(await trustDecision.json(), challengeDocument);
    assert.equal(trustDecision.headers.get("X-Agent-Guild-Relay"), "non-custodial");
    assert.match(
      trustDecision.headers.get("X-Agent-Guild-Canonical-Resource") ?? "",
      /\/check\?capability=fact-check&signed=true&ttl_seconds=3600$/,
    );
    assert.equal(calls[0].requestHeaders.get("x-api-key"), null);
    assert.equal(
      calls[0].requestHeaders.get("payment-signature"),
      "buyer-payment-signature",
    );

    const envelope = await renderRequest("/envelopes/issue", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-guild-caller-proof": "caller-bound-proof",
        "PAYMENT-SIGNATURE": "buyer-envelope-payment",
        "X-API-Key": "sandbox-credential-must-not-cross-relay",
      },
      body: envelopeBody,
    });
    assert.equal(envelope.status, 200);
    assert.equal(envelope.headers.get("PAYMENT-RESPONSE"), "base-mainnet-receipt");
    assert.equal(calls[1].requestBody, envelopeBody);
    assert.equal(
      calls[1].requestHeaders.get("x-guild-caller-proof"),
      "caller-bound-proof",
    );
    assert.equal(calls[1].requestHeaders.get("x-api-key"), null);

    const decisionBody = JSON.stringify({
      payment: {
        scheme: "exact",
        network: "eip155:8453",
        asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        amount: "10000",
        pay_to: "0x1111111111111111111111111111111111111111",
        resource: "https://seller.example/api/research",
      },
      capability: "research",
      policy: { max_risk: 32.99, min_confidence: 0.5 },
    });
    const paymentDecision = await renderRequest("/wallet-binding/decision", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "PAYMENT-SIGNATURE": "buyer-policy-payment",
        "X-API-Key": "sandbox-credential-must-not-cross-relay",
      },
      body: decisionBody,
    });
    assert.equal(paymentDecision.status, 402);
    assert.equal(calls[2].requestBody, decisionBody);
    assert.equal(calls[2].requestHeaders.get("x-api-key"), null);
    assert.equal(
      calls[2].requestUrl,
      "https://agent-guild-5d5r.onrender.com/wallet-binding/decision",
    );

    const unsupported = await renderRequest(
      "/trust-decision?capability=manufactured-reputation",
    );
    assert.equal(unsupported.status, 422);
    assert.equal(calls.length, 3);

    const wrongMethod = await renderRequest("/trust-decision", { method: "POST" });
    assert.equal(wrongMethod.status, 405);
    assert.equal(wrongMethod.headers.get("allow"), "GET, OPTIONS");
    assert.equal(calls.length, 3);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("binds a Base ERC-8004 identity to a paid Agent Guild preflight", async () => {
  const { publicKey, privateKey } = generateKeyPairSync("ed25519");
  const privateKeyBase64 = privateKey
    .export({ format: "der", type: "pkcs8" })
    .toString("base64");
  const publicJwk = publicKey.export({ format: "jwk" });
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("erc8004", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const originalFetch = globalThis.fetch;
  const agentRegistry =
    "eip155:8453:0x8004A169FB4a3325136EB29fA0ceB6D2e539a432";
  const registration = {
    type: "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
    name: "Example autonomous agent",
    description: "A live machine service",
    services: [
      {
        name: "A2A",
        endpoint: "https://agent.example/.well-known/agent-card.json",
        version: "0.3.0",
      },
    ],
    x402Support: true,
    active: true,
    registrations: [{ agentId: 7, agentRegistry }],
    supportedTrust: ["reputation"],
  };
  const calls = [];

  try {
    globalThis.fetch = async (input, init = {}) => {
      const target = input instanceof Request ? input.url : String(input);
      calls.push({ target, init });
      if (target === "https://mainnet.base.org") {
        const batch = JSON.parse(String(init.body));
        return Response.json(
          batch.map((rpc) => {
            const data = rpc.params[0].data;
            let result;
            if (data.startsWith("0x6352211e")) {
              result =
                "0x0000000000000000000000001111111111111111111111111111111111111111";
            } else if (data.startsWith("0xc87b56dd")) {
              result = encodeAbiString("https://registry.example/agent-7.json");
            } else if (data.startsWith("0x00339509")) {
              result =
                "0x0000000000000000000000002222222222222222222222222222222222222222";
            } else {
              throw new Error(`unexpected eth_call ${data}`);
            }
            return { jsonrpc: "2.0", id: rpc.id, result };
          }),
        );
      }
      if (target === "https://registry.example/agent-7.json") {
        return Response.json(registration);
      }
      if (
        target ===
        "https://agent.example/.well-known/agent-registration.json"
      ) {
        return Response.json({ registrations: registration.registrations });
      }
      if (target.startsWith("https://agent-guild-5d5r.onrender.com/preflight/deep?")) {
        const headers = new Headers(init.headers);
        assert.equal(headers.get("x-api-key"), null);
        if (!headers.get("payment-signature")) {
          return new Response(JSON.stringify({ x402Version: 2, accepts: [{}] }), {
            status: 402,
            headers: {
              "content-type": "application/json",
              "PAYMENT-REQUIRED": "erc8004-mainnet-challenge",
            },
          });
        }
        return Response.json(
          {
            verdict: "allow",
            endpoint: "https://agent.example/.well-known/agent-card.json",
            risk: 9,
          },
          { headers: { "PAYMENT-RESPONSE": "erc8004-mainnet-receipt" } },
        );
      }
      throw new Error(`unexpected upstream ${target}`);
    };

    const env = {
      ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
      WORKER_ED25519_PRIVATE_KEY_PKCS8_B64: privateKeyBase64,
      WORKER_ED25519_PUBLIC_JWK_JSON: JSON.stringify(publicJwk),
    };
    const ctx = { waitUntil() {}, passThroughOnException() {} };

    const challenge = await worker.fetch(
      new Request("http://localhost/erc8004/preflight?agent_id=7", {
        headers: { "X-API-Key": "must-not-cross-relay" },
      }),
      env,
      ctx,
    );
    assert.equal(challenge.status, 402);
    assert.equal(
      challenge.headers.get("PAYMENT-REQUIRED"),
      "erc8004-mainnet-challenge",
    );
    assert.equal(challenge.headers.get("X-ERC8004-Agent-Id"), "7");
    assert.equal(challenge.headers.get("X-ERC8004-Agent-Registry"), agentRegistry);

    const paid = await worker.fetch(
      new Request("http://localhost/erc8004/preflight?agent_id=7", {
        headers: { "PAYMENT-SIGNATURE": "buyer-mainnet-payment" },
      }),
      env,
      ctx,
    );
    assert.equal(paid.status, 200);
    assert.equal(paid.headers.get("PAYMENT-RESPONSE"), "erc8004-mainnet-receipt");
    const artifact = await paid.json();
    assert.equal(artifact.type, "agent-guild/erc8004-preflight/v1");
    assert.equal(artifact.subject.agent_id, "7");
    assert.equal(artifact.subject.owner, "0x1111111111111111111111111111111111111111");
    assert.equal(
      artifact.subject.agent_wallet,
      "0x2222222222222222222222222222222222222222",
    );
    assert.equal(
      artifact.subject.service.endpoint,
      "https://agent.example/.well-known/agent-card.json",
    );
    assert.equal(artifact.subject.endpoint_domain_verification.status, "well-known");
    assert.equal(artifact.agent_guild.result.verdict, "allow");
    assert.equal(artifact.agent_guild.payment_response, "erc8004-mainnet-receipt");
    assert.match(artifact.issuer.boundary, /not an ERC-8004 Validation Registry claim/);

    const signature = artifact.signatures[0];
    const unsigned = { ...artifact };
    delete unsigned.signatures;
    const payload = Buffer.from(canonicalize(unsigned)).toString("base64url");
    assert.equal(
      verify(
        null,
        Buffer.from(`${signature.protected}.${payload}`),
        publicKey,
        Buffer.from(signature.signature, "base64url"),
      ),
      true,
    );
    assert.equal(
      calls.filter(({ target }) => target === "https://mainnet.base.org").length,
      2,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
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

    const jwksResponse = await worker.fetch(
      new Request("http://localhost/.well-known/jwks.json"),
      env,
      { waitUntil() {}, passThroughOnException() {} },
    );
    const jwks = await jwksResponse.json();
    assert.equal(jwks.keys[0].kid, "ed25519-worker-1");
    assert.equal(jwks.keys[0].alg, "EdDSA");
    assert.equal(jwks.keys[0].x, publicJwk.x);

    const cardResponse = await worker.fetch(
      new Request("http://localhost/.well-known/agent-card.json"),
      env,
      { waitUntil() {}, passThroughOnException() {} },
    );
    const signedCard = await cardResponse.json();
    assert.equal(signedCard.signatures.length, 1);
    const cardSignature = signedCard.signatures[0];
    const protectedHeader = JSON.parse(
      Buffer.from(cardSignature.protected, "base64url").toString("utf8"),
    );
    assert.equal(protectedHeader.alg, "EdDSA");
    assert.equal(protectedHeader.typ, "JOSE");
    assert.equal(protectedHeader.kid, "ed25519-worker-1");
    assert.equal(protectedHeader.jku, "http://localhost/.well-known/jwks.json");
    const unsignedCard = structuredClone(signedCard);
    delete unsignedCard.signatures;
    const cardPayload = Buffer.from(canonicalize(unsignedCard)).toString(
      "base64url",
    );
    assert.equal(
      verify(
        null,
        Buffer.from(`${cardSignature.protected}.${cardPayload}`),
        publicKey,
        Buffer.from(cardSignature.signature, "base64url"),
      ),
      true,
    );
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
  assert.equal(
    message.paid_action.call,
    "GET https://codex-autonomous-worker.rwdburley.chatgpt.site/trust-decision?capability=fact-check",
  );
  assert.match(
    message.paid_action.canonical_resource,
    /capability=fact-check&signed=true&ttl_seconds=3600$/,
  );
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
    /\/trust-decision\?capability=coding$/,
  );
  assert.match(
    message.paid_action.canonical_resource,
    /\/check\?capability=coding&signed=true&ttl_seconds=3600$/,
  );
});
