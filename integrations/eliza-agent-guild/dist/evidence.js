import { record, requireValue } from "./policy.js";

const CHECKS = [
	"endpoint_reachable",
	"protocol_handshake",
	"agent_card_resolves",
	"agent_card_signed",
	"payment_claim_holds",
	"independent_evidence",
];
const VERDICTS = [
	"do_not_delegate",
	"delegate_with_caution",
	"no_failed_checks",
];

export function preflightEvidence(value, target, requestedAt) {
	requireValue(record(value) && value.target === target, "target_mismatch");
	requireValue(
		VERDICTS.includes(value.verdict) &&
			Array.isArray(value.checks) &&
			value.checks.length > 0 &&
			value.checks.length <= CHECKS.length,
		"unsupported_response",
	);
	const seen = new Set();
	const checks = value.checks.map((item) => {
		requireValue(
			record(item) &&
				CHECKS.includes(item.check) &&
				!seen.has(item.check) &&
				["proven", "failed", "unknown"].includes(item.status),
			"unsupported_response",
		);
		seen.add(item.check);
		return { check: item.check, status: item.status };
	});
	// The current six checks are required so a missing check cannot disappear silently.
	requireValue(seen.size === CHECKS.length, "incomplete_response");
	const failed = checks
		.filter((item) => item.status === "failed")
		.map((item) => item.check);
	const unknowns = checks
		.filter((item) => item.status === "unknown")
		.map((item) => item.check);
	for (const [key, expected] of [
		["failed", failed],
		["unknowns", unknowns],
	]) {
		requireValue(
			Array.isArray(value[key]) &&
				value[key].length === expected.length &&
				new Set(value[key]).size === expected.length &&
				value[key].every((item) => expected.includes(item)),
			"inconsistent_response",
		);
	}
	const block = failed.some((check) =>
		["endpoint_reachable", "protocol_handshake"].includes(check),
	);
	const expectedVerdict = block
		? "do_not_delegate"
		: failed.length > 0
			? "delegate_with_caution"
			: "no_failed_checks";
	requireValue(value.verdict === expectedVerdict, "inconsistent_response");
	return {
		operation: "endpoint_observation",
		target,
		requestedAt,
		verdict: value.verdict,
		checks,
		failed,
		unknowns,
		disposition: block ? "block" : "caution",
		limitations: [
			"Service-reported observations of the exact requested URL; no later execution is bound to this result.",
			"A protocol handshake does not prove task execution. Card-signature presence does not verify the signature.",
			"Paid operations, private data handling and task quality are unverified. Unknown checks remain unknown.",
			"No delegation or payment is authorized. No subject DID is bound to this endpoint.",
		],
	};
}

export function verificationEvidence(value, entry, config, requestedAt) {
	requireValue(
		record(value) &&
			typeof value.valid === "boolean" &&
			typeof value.guild_issued === "boolean",
		"unsupported_response",
	);
	requireValue(value.issuer === config.expectedIssuerDid, "issuer_mismatch");
	requireValue(
		value.subject_did === entry.expectedSubjectDid,
		"subject_mismatch",
	);
	return {
		operation: "passport_verification",
		requestedAt,
		subjectDid: entry.expectedSubjectDid,
		issuerDid: config.expectedIssuerDid,
		signatureValidReported: value.valid,
		guildIssuedReported: value.guild_issued,
		verified: value.valid && value.guild_issued,
		validFrom: entry.credential.validFrom,
		validUntil: entry.credential.validUntil,
		timeAndBindingChecksPassed: true,
		disposition:
			value.valid && value.guild_issued
				? "verified_origin_and_integrity_only"
				: "unverified",
		limitations: [
			"Remote verification relies on the configured Guild service. It is not an independent local cryptographic verification.",
			"Signature validity proves origin and integrity, not truth, safety, task quality or control of an endpoint.",
			"Expected issuer and subject were supplied separately by the caller; their independence is a caller precondition, not inferable from matching values.",
			"No enrollment, issuance, payment or delegation was performed.",
		],
	};
}
