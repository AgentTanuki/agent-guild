import {
	DEADLINE_MS,
	GuildError,
	MAX_RESPONSE_BYTES,
	ORIGIN,
	requireValue,
} from "./policy.js";

// No caller-controlled origin, authorization header, redirect, cookie jar or retry.
export async function requestJson(
	path,
	body,
	fetchImpl = globalThis.fetch,
	deadlineMs = DEADLINE_MS,
) {
	const controller = new AbortController();
	let reader;
	let timer;
	const timeout = new Promise((_, reject) => {
		timer = setTimeout(() => {
			controller.abort();
			reject(new GuildError("request_timeout"));
		}, deadlineMs);
	});
	const operation = (async () => {
		const response = await fetchImpl(`${ORIGIN}${path}`, {
			method: body === undefined ? "GET" : "POST",
			headers: {
				Accept: "application/json",
				"User-Agent": "elizaos-plugin-agent-guild/0.1.0-dev.0 (host=elizaOS)",
				...(body === undefined ? {} : { "Content-Type": "application/json" }),
			},
			body: body === undefined ? undefined : JSON.stringify(body),
			redirect: "error",
			credentials: "omit",
			referrerPolicy: "no-referrer",
			signal: controller.signal,
		});
		if (controller.signal.aborted) {
			if (response.body) void response.body.cancel().catch(() => {});
			throw new GuildError("request_timeout");
		}
		requireValue(
			response.status >= 200 && response.status < 300 && !response.redirected,
			"http_unavailable",
		);
		// Response.url is empty for local Response fixtures; native fetch supplies it.
		requireValue(
			!response.url || response.url === `${ORIGIN}${path}`,
			"unexpected_response_url",
		);
		requireValue(
			/^application\/json(?:\s*;|$)/i.test(
				response.headers.get("content-type") ?? "",
			),
			"invalid_content_type",
		);
		const length = response.headers.get("content-length");
		requireValue(
			length === null ||
				(/^\d+$/.test(length) && Number(length) <= MAX_RESPONSE_BYTES),
			"response_too_large",
		);
		requireValue(response.body, "empty_response");
		reader = response.body.getReader();
		const chunks = [];
		let size = 0;
		while (true) {
			const { done, value } = await reader.read();
			if (done) break;
			size += value.byteLength;
			requireValue(size <= MAX_RESPONSE_BYTES, "response_too_large");
			chunks.push(value);
		}
		try {
			return JSON.parse(
				new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)),
			);
		} catch {
			throw new GuildError("invalid_json");
		}
	})();
	try {
		return await Promise.race([operation, timeout]);
	} catch (error) {
		throw error instanceof GuildError
			? error
			: new GuildError("request_unavailable");
	} finally {
		clearTimeout(timer);
		controller.abort();
		if (reader) void reader.cancel().catch(() => {});
	}
}
