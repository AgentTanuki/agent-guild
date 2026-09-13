"""Two optional native Agently Actions. Importing or mounting performs no HTTP."""

from __future__ import annotations

import math
from datetime import datetime, timezone

from .transport import ORIGIN, USER_AGENT, Unavailable, request as guild_request
from .validation import InvalidInput, check_time, did, exact_object, passport, public_url, require, timestamp

CHECKS = (
    "endpoint_reachable",
    "protocol_handshake",
    "agent_card_resolves",
    "agent_card_signed",
    "payment_claim_holds",
    "independent_evidence",
)
ACTION_IDS = ("guild_preflight", "guild_verify_public_passport")
LIMITATIONS = [
    "Optional observations only; no authorization, endpoint ownership, task quality or safety guarantee.",
    "Guild receives the selected public URL and actively probes it, or receives the complete supplied public credential.",
    "POST /credentials/verify records passport_verified, including unsuccessful verification; host logs are separate.",
    "No target invocation, registration, credential issuance, payment or paid fallback is performed here.",
    "Remote prose is omitted. A response digest and local times do not authenticate remote assertions.",
    "The deadline bounds waiting and sends cancellation; an arbitrary host transport can outlive it if uncooperative.",
]


def observation(value, target):
    """Project the six complete statuses and reject contradictory summaries/verdicts."""
    try:
        require(type(value) is dict and value.get("target") == target)
        checks = value.get("checks")
        require(type(checks) is list and len(checks) == len(CHECKS))
        seen = {}
        for item in checks:
            require(type(item) is dict)
            name, status = item.get("check"), item.get("status")
            require(type(name) is str and name in CHECKS and name not in seen)
            require(type(status) is str and status in {"proven", "failed", "unknown"})
            seen[name] = status
        projected = [{"check": name, "status": seen[name]} for name in CHECKS]
        expected = {
            "failed": [name for name in CHECKS if seen[name] == "failed"],
            "unknowns": [name for name in CHECKS if seen[name] == "unknown"],
            "scored": [name for name in CHECKS if seen[name] != "unknown"],
        }
        for key, names in expected.items():
            actual = value.get(key)
            require(type(actual) is list and all(type(item) is str for item in actual))
            require(len(actual) == len(set(actual)) and set(actual) == set(names))
        if any(seen[name] == "failed" for name in CHECKS[:2]):
            verdict = "do_not_delegate"
        else:
            verdict = "delegate_with_caution" if expected["failed"] else "no_failed_checks"
        require(value.get("verdict") == verdict)
    except InvalidInput as error:
        raise Unavailable("invalid_observation", 200) from error
    return {
        "target": target,
        "checks": projected,
        **expected,
        "verdict": verdict,
        "scope": [
            "Unknown checks stay unresolved even when no performed check failed.",
            "Card-signature presence is not signature verification; a payment observation is not settlement proof.",
            "Independent ownership remains unverified. Later execution is not bound to this observation.",
        ],
    }


class GuildActions:
    """A normal use_actions package with host-owned restrictions and HTTP transport."""

    def __init__(
        self,
        *,
        timeout=15.0,
        max_passport_age_seconds=86400,
        allowed_hosts=None,
        expected_issuer_did=None,
        transport_factory=None,
    ):
        require(type(timeout) in (int, float) and math.isfinite(timeout) and 0.1 <= timeout <= 45, "invalid_timeout")
        require(type(max_passport_age_seconds) is int and 1 <= max_passport_age_seconds <= 604800, "invalid_max_age")
        if allowed_hosts is not None:
            require(type(allowed_hosts) in (list, tuple), "invalid_allowed_hosts")
            for host in allowed_hosts:
                require(type(host) is str and host == host.lower(), "invalid_allowed_hosts")
                require(public_url("https://" + host) == "https://" + host, "invalid_allowed_hosts")
                require("/" not in host and "?" not in host and ":" not in host, "invalid_allowed_hosts")
            allowed_hosts = frozenset(allowed_hosts)
        self.timeout = float(timeout)
        self.max_age = max_passport_age_seconds
        self.allowed_hosts = allowed_hosts
        self.expected_issuer = did(expected_issuer_did) if expected_issuer_did is not None else None
        require(transport_factory is None or callable(transport_factory), "invalid_transport_factory")
        self.transport_factory = transport_factory

    def register_actions(self, action, *, tags=None):
        """Called by real agent.use_actions; no custom dispatcher or fake framework type."""
        for action_id, handler, description in (
            (
                ACTION_IDS[0],
                self.preflight,
                "Disclose one selected public HTTP(S) endpoint to Guild for active observation. "
                "request must be exactly {url: string}. Return six statuses/unknowns, not delegation authority.",
            ),
            (
                ACTION_IDS[1],
                self.verify_public_passport,
                "Disclose a supplied PUBLIC credential unchanged to Guild for online verification. "
                "request must contain credential, expected_issuer_did and expected_subject_did. "
                "Expected DIDs must be independently chosen. Verification logs passport_verified even on failure.",
            ),
        ):
            action.register_action(
                action_id=action_id,
                desc=description,
                kwargs={
                    "request": (dict, "Complete public business input object; never secrets or unrelated context.")
                },
                required_input_keys=["request"],
                func=handler,
                returns=dict,
                tags=tags,
                side_effect_level="write",
                replay_safe=False,
                concurrency_mode="parallel",
                default_policy={"timeout_seconds": self.timeout + 1},
            )
        return list(ACTION_IDS)

    @staticmethod
    def _result(started, route, status, *, code=None, verified=False, **fields):
        return {
            "status": status,
            "verified": verified,
            "code": code,
            "requested_at": started,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "service_url": ORIGIN + route,
            "user_agent": USER_AGENT,
            "limitations": list(LIMITATIONS),
            **fields,
        }

    async def preflight(self, request=None, **unexpected):
        """Observe one public endpoint; nested unexpected input is rejected on every dispatch path."""
        started = datetime.now(timezone.utc).isoformat()
        route = "/preflight"
        try:
            require(not unexpected)
            exact_object(request, {"url"})
            target = public_url(request["url"], self.allowed_hosts)
        except InvalidInput as error:
            return self._result(started, route, "rejected", code=str(error))
        try:
            value, provenance = await guild_request(
                "GET", route, timeout=self.timeout, params={"url": target}, transport_factory=self.transport_factory
            )
            return self._result(started, route, "observed", **observation(value, target), **provenance)
        except Unavailable as error:
            return self._result(started, route, "unavailable", code=str(error), target=target, http_status=error.status)

    async def verify_public_passport(self, request=None, **unexpected):
        """Verify a supplied public object; action success alone never means passport verification."""
        started = datetime.now(timezone.utc).isoformat()
        route = "/credentials/verify"
        try:
            require(not unexpected)
            exact_object(request, {"credential", "expected_issuer_did", "expected_subject_did"})
            credential, raw, issuer, subject = passport(
                request["credential"], request["expected_issuer_did"], request["expected_subject_did"]
            )
            require(self.expected_issuer is None or self.expected_issuer == issuer, "host_issuer_mismatch")
            check_time(credential, self.max_age)
        except InvalidInput as error:
            return self._result(started, route, "rejected", code=str(error))
        try:
            value, provenance = await guild_request(
                "POST", route, timeout=self.timeout, body=raw, transport_factory=self.transport_factory
            )
            if not (
                type(value) is dict
                and type(value.get("valid")) is bool
                and type(value.get("guild_issued")) is bool
                and value.get("issuer") == issuer
                and value.get("subject_did") == subject
            ):
                raise Unavailable("invalid_verification_response", 200)
            now = datetime.now(timezone.utc)
            valid_from, valid_until = timestamp(credential["validFrom"]), timestamp(credential["validUntil"])
            validity = valid_from <= now < valid_until
            freshness = 0 <= (now - valid_from).total_seconds() <= self.max_age
            verified = value["valid"] and value["guild_issued"] and validity and freshness
            return self._result(
                started,
                route,
                "completed",
                verified=verified,
                signature_valid_reported=value["valid"],
                guild_issued_reported=value["guild_issued"],
                signed_validity_passed=validity,
                freshness_passed=freshness,
                expected_issuer_did=issuer,
                expected_subject_did=subject,
                valid_from=credential["validFrom"],
                valid_until=credential["validUntil"],
                max_passport_age_seconds=self.max_age,
                time_checked_at=now.isoformat(),
                verification_scope="Online verifier flags; no independent local signature verification or endpoint binding.",
                **provenance,
            )
        except Unavailable as error:
            return self._result(started, route, "unavailable", code=str(error), http_status=error.status)
