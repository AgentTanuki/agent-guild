import { preflightEvidence, verificationEvidence } from "./evidence.js";
import {
	configure,
	did,
	GuildError,
	jsonSnapshot,
	MAX_PASSPORT_BYTES,
	parameters,
	passportBinding,
	publicEndpoint,
	record,
	requireValue,
} from "./policy.js";
import { requestJson } from "./transport.js";

export function makeActions(explicitConfig) {
	// Factory configuration is captured now, before any model-selected invocation.
	const fixed =
		explicitConfig === undefined ? undefined : configure(explicitConfig);
	const runtimeConfigs = new WeakMap();
	function policy(runtime) {
		if (fixed) return fixed;
		if (!runtime || typeof runtime.getSetting !== "function")
			return configure({});
		if (!runtimeConfigs.has(runtime)) {
			const raw = runtime.getSetting("AGENT_GUILD_CONFIG");
			// Snapshot once per runtime. A new plugin instance is needed to apply changes.
			runtimeConfigs.set(
				runtime,
				configure(raw === undefined || raw === null || raw === "" ? {} : raw),
			);
		}
		return runtimeConfigs.get(runtime);
	}
	const descriptors = [
		{
			name: "AGENT_GUILD_PREFLIGHT",
			parameters: [
				{
					name: "url",
					description:
						"Exact caller-selected public HTTP or HTTPS endpoint without credentials, query or fragment. This triggers an active public probe.",
					required: true,
					schema: { type: "string", minLength: 1, maxLength: 2048 },
				},
			],
			description:
				"Actively probe one caller-selected public endpoint using Agent Guild. Returns measured statuses and unknowns; never authorizes delegation or payment.",
			available: (config) =>
				config.allowedEndpoints === undefined ||
				config.allowedEndpoints.size > 0,
			async perform(config, params) {
				const value = params.url;
				publicEndpoint(value);
				requireValue(
					config.allowedEndpoints === undefined ||
						config.allowedEndpoints.has(value),
					"endpoint_not_approved",
				);
				const requestedAt = new Date().toISOString();
				const response = await requestJson(
					`/preflight?url=${encodeURIComponent(value)}`,
				);
				return preflightEvidence(response, value, requestedAt);
			},
		},
		{
			name: "AGENT_GUILD_VERIFY_PASSPORT",
			parameters: [
				{
					name: "credential",
					description:
						"The unchanged, caller-selected PUBLIC Agent Guild passport object. Never pass confidential claims or unrelated conversation data.",
					required: true,
					schema: { type: "object" },
				},
				{
					name: "expected_issuer_did",
					description:
						"Issuer did:key independently selected by the caller; do not infer it from this untrusted credential.",
					required: true,
					schema: { type: "string", minLength: 39, maxLength: 109 },
				},
				{
					name: "expected_subject_did",
					description:
						"Intended subject did:key independently selected by the caller; do not infer it from this untrusted credential.",
					required: true,
					schema: { type: "string", minLength: 39, maxLength: 109 },
				},
			],
			description:
				"Verify a caller-supplied public passport with separately supplied expected issuer and subject DIDs and signed freshness checks. Never fetches, issues or registers an identity.",
			available: () => true,
			async perform(config, params) {
				const expectedIssuerDid = did(params.expected_issuer_did);
				requireValue(
					config.expectedIssuerDid === undefined ||
						expectedIssuerDid === config.expectedIssuerDid,
					"issuer_not_approved",
				);
				requireValue(record(params.credential), "invalid_credential");
				const credential = jsonSnapshot(
					params.credential,
					MAX_PASSPORT_BYTES,
					"invalid_credential",
				);
				requireValue(record(credential), "invalid_credential");
				const entry = {
					credential,
					expectedSubjectDid: did(params.expected_subject_did),
				};
				const bindingConfig = { ...config, expectedIssuerDid };
				passportBinding(entry, bindingConfig, Date.now());
				const requestedAt = new Date().toISOString();
				const response = await requestJson(
					"/credentials/verify",
					entry.credential,
				);
				// Recheck expiry and maximum age after network time has elapsed.
				passportBinding(entry, bindingConfig, Date.now());
				return verificationEvidence(
					response,
					entry,
					bindingConfig,
					requestedAt,
				);
			},
		},
	];
	return descriptors.map((descriptor) => ({
		name: descriptor.name,
		description: descriptor.description,
		parameters: descriptor.parameters,
		validate: async (runtime) => {
			try {
				return descriptor.available(policy(runtime));
			} catch {
				return false;
			}
		},
		handler: async (runtime, _message, _state, options, callback) => {
			let result;
			try {
				const config = policy(runtime);
				const value = parameters(
					options,
					descriptor.parameters.map((item) => item.name),
				);
				const data = await descriptor.perform(config, value);
				result = { success: true, text: JSON.stringify(data), data };
			} catch (error) {
				const code =
					error instanceof GuildError ? error.code : "operation_unavailable";
				const data = {
					operation: descriptor.name,
					status: "unavailable",
					code,
					verified: false,
					disposition: "caution",
				};
				result = {
					success: false,
					error: code,
					text: JSON.stringify(data),
					data,
				};
			}
			if (callback)
				await callback(
					{ text: result.text, actions: [descriptor.name] },
					descriptor.name,
				);
			return result;
		},
	}));
}
