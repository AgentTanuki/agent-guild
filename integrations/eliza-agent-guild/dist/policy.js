export const ORIGIN = "https://agent-guild-5d5r.onrender.com";
export const MAX_RESPONSE_BYTES = 65536;
export const MAX_PASSPORT_BYTES = 32768;
export const DEADLINE_MS = 45000;

export class GuildError extends Error {
	constructor(code) {
		super(code);
		this.code = code;
	}
}
export function requireValue(condition, code) {
	if (!condition) throw new GuildError(code);
}
export function record(value) {
	return value !== null && typeof value === "object" && !Array.isArray(value);
}
export function exactKeys(value, allowed, code) {
	requireValue(
		record(value) && Object.keys(value).every((key) => allowed.includes(key)),
		code,
	);
}
export function jsonSnapshot(value, cap, code) {
	try {
		const serialized =
			typeof value === "string" ? value : JSON.stringify(value);
		requireValue(
			typeof serialized === "string" && Buffer.byteLength(serialized) <= cap,
			code,
		);
		return JSON.parse(serialized);
	} catch {
		throw new GuildError(code);
	}
}

// This is lexical screening, not DNS resolution or a proof that a path is public.
// The host operator remains responsible for selecting public, non-secret URLs.
export function publicEndpoint(value) {
	requireValue(
		typeof value === "string" && value.length <= 2048,
		"invalid_endpoint",
	);
	let url;
	try {
		url = new URL(value);
	} catch {
		throw new GuildError("invalid_endpoint");
	}
	requireValue(
		["http:", "https:"].includes(url.protocol) &&
			!url.username &&
			!url.password &&
			!url.search &&
			!url.hash &&
			!url.port,
		"invalid_endpoint",
	);
	const labels = url.hostname.split(".");
	requireValue(
		labels.length >= 2 &&
			labels.every((part) =>
				/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(part),
			),
		"invalid_endpoint",
	);
	requireValue(
		/^[a-z]{2,63}$/.test(labels.at(-1)) &&
			![
				"local",
				"localhost",
				"internal",
				"test",
				"invalid",
				"onion",
				"home",
				"lan",
			].includes(labels.at(-1)),
		"invalid_endpoint",
	);
	requireValue(value === url.href, "noncanonical_endpoint");
	return value;
}
export function did(value) {
	requireValue(
		typeof value === "string" &&
			/^did:key:z[1-9A-HJ-NP-Za-km-z]{30,100}$/.test(value),
		"invalid_did",
	);
	return value;
}
export function configure(raw) {
	const config = jsonSnapshot(raw, 262144, "invalid_config");
	exactKeys(
		config,
		["allowedEndpoints", "expectedIssuerDid", "maxPassportAgeSeconds"],
		"invalid_config",
	);
	const endpoints = config.allowedEndpoints;
	requireValue(
		endpoints === undefined ||
			(Array.isArray(endpoints) && endpoints.length <= 32),
		"invalid_config",
	);
	const allowedEndpoints =
		endpoints === undefined
			? undefined
			: new Set(endpoints.map(publicEndpoint));
	const expectedIssuerDid =
		config.expectedIssuerDid === undefined
			? undefined
			: did(config.expectedIssuerDid);
	const maxPassportAgeSeconds = config.maxPassportAgeSeconds ?? 86400;
	requireValue(
		Number.isInteger(maxPassportAgeSeconds) &&
			maxPassportAgeSeconds >= 1 &&
			maxPassportAgeSeconds <= 604800,
		"invalid_config",
	);
	return { allowedEndpoints, expectedIssuerDid, maxPassportAgeSeconds };
}

export function parameters(options, keys) {
	requireValue(
		record(options) && !options.parameterErrors?.length,
		"invalid_parameters",
	);
	requireValue(record(options.parameters), "invalid_parameters");
	const value = jsonSnapshot(options.parameters, 40960, "invalid_parameters");
	exactKeys(value, keys, "invalid_parameters");
	requireValue(
		keys.every((key) => Object.hasOwn(value, key)),
		"invalid_parameters",
	);
	return value;
}

// Strict UTC calendar validation: Date.parse alone normalizes impossible dates.
function timestamp(value) {
	requireValue(typeof value === "string", "invalid_credential_time");
	const match =
		/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(?:Z|\+00:00)$/.exec(
			value,
		);
	requireValue(match, "invalid_credential_time");
	const millis = Date.parse(
		`${match[1]}.${(match[2] ?? "").padEnd(3, "0").slice(0, 3)}Z`,
	);
	requireValue(
		Number.isFinite(millis) &&
			new Date(millis).toISOString().slice(0, 19) === match[1],
		"invalid_credential_time",
	);
	return millis;
}
export function passportBinding(entry, config, now) {
	const credential = entry.credential;
	requireValue(
		credential.issuer === config.expectedIssuerDid,
		"issuer_mismatch",
	);
	requireValue(
		record(credential.credentialSubject) &&
			credential.credentialSubject.id === entry.expectedSubjectDid,
		"subject_mismatch",
	);
	requireValue(
		Array.isArray(credential.type) &&
			credential.type.includes("VerifiableCredential") &&
			credential.type.includes("AgentGuildPassport"),
		"unsupported_credential",
	);
	requireValue(
		record(credential.proof) &&
			credential.proof.type === "DataIntegrityProof" &&
			credential.proof.cryptosuite === "eddsa-jcs-2022",
		"unsupported_proof",
	);
	requireValue(
		credential.proof.proofPurpose === "assertionMethod" &&
			typeof credential.proof.proofValue === "string" &&
			/^z[1-9A-HJ-NP-Za-km-z]+$/.test(credential.proof.proofValue),
		"unsupported_proof",
	);
	const expectedMethod = `${config.expectedIssuerDid}#${config.expectedIssuerDid.slice(8)}`;
	requireValue(
		credential.proof.verificationMethod === expectedMethod,
		"issuer_mismatch",
	);
	const from = timestamp(credential.validFrom);
	const until = timestamp(credential.validUntil);
	requireValue(
		Number.isFinite(now) && from <= now && now < until && from < until,
		"credential_outside_validity",
	);
	requireValue(
		now - from <= config.maxPassportAgeSeconds * 1000,
		"credential_stale",
	);
}
