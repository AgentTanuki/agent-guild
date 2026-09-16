import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { createAgentGuildPlugin } from "../dist/index.js";

globalThis.fetch = async () => {
	throw new Error("Unexpected network call in offline tests");
};
const fixture = JSON.parse(
	await readFile(new URL("./fixtures/owned-preflight.json", import.meta.url)),
);
const run = (body, t, url = body.target) => {
	t.mock.method(globalThis, "fetch", async (requestUrl) => {
		assert.equal(
			new URL(requestUrl).origin,
			"https://agent-guild-5d5r.onrender.com",
		);
		return new Response(JSON.stringify(body), {
			headers: { "Content-Type": "application/json" },
		});
	});
	return createAgentGuildPlugin().actions[0].handler({}, {}, undefined, {
		parameters: { url },
	});
};

test("new public HTTP target is accepted while Guild transport stays HTTPS", async (t) => {
	const target = "http://example.com/mcp";
	assert.equal((await run({ ...fixture, target }, t)).success, true);
});

test("failed reachability and handshake each require the blocking service verdict", async (t) => {
	for (const check of ["endpoint_reachable", "protocol_handshake"]) {
		const body = structuredClone(fixture);
		body.checks.find((value) => value.check === check).status = "failed";
		body.failed.push(check);
		assert.equal((await run(body, t)).error, "inconsistent_response");
		body.verdict = "do_not_delegate";
		const result = await run(body, t);
		assert.equal(result.success, true);
		assert.equal(result.data.disposition, "block");
	}
});

test("unknown checks stay unknown and do not change the service no-failures verdict", async (t) => {
	const body = structuredClone(fixture);
	body.checks.find((value) => value.check === "agent_card_signed").status =
		"proven";
	body.failed = [];
	assert.equal((await run(body, t)).error, "inconsistent_response");
	body.verdict = "no_failed_checks";
	const result = await run(body, t);
	assert.equal(result.data.verdict, "no_failed_checks");
	assert.deepEqual(result.data.unknowns, [
		"payment_claim_holds",
		"independent_evidence",
	]);
	assert.equal(result.data.disposition, "caution");
	body.verdict = "do_not_delegate";
	assert.equal((await run(body, t)).error, "inconsistent_response");
});
