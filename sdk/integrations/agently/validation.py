"""Bounded business input checks; Agently call schemas are not these guards."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sdk.agentguild_verify import public_key_from_did

MAX_CREDENTIAL_BYTES = 32768
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class InvalidInput(ValueError):
    """Fixed local input failure code; never remote prose."""


def require(condition, code="invalid_input"):
    if not condition:
        raise InvalidInput(code)


def exact_object(value, keys):
    require(type(value) is dict and all(type(key) is str for key in value) and set(value) == set(keys))
    return value


def public_url(value, allowed_hosts=None):
    require(type(value) is str and 1 <= len(value) <= 2048, "invalid_public_url")
    require(value.startswith(("https://", "http://")), "invalid_public_url")
    require(not any(ord(c) <= 32 or ord(c) == 127 for c in value), "invalid_public_url")
    require("\\" not in value and "#" not in value, "invalid_public_url")
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        require(parts.username is None and parts.password is None, "invalid_public_url")
        require(parts.port is None or 1 <= parts.port <= 65535, "invalid_public_url")
        host.encode("ascii")
    except (ValueError, UnicodeError) as error:
        raise InvalidInput("invalid_public_url") from error
    labels = host.split(".")
    require(
        len(host) <= 253
        and len(labels) >= 2
        and not labels[-1].isdigit()
        and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
        and labels[-1] not in {"local", "localhost", "internal", "invalid", "test", "example", "onion"},
        "invalid_public_hostname",
    )
    require(allowed_hosts is None or host in allowed_hosts, "host_not_allowed")
    return value


def did(value):
    require(type(value) is str and re.fullmatch(r"did:key:z[1-9A-HJ-NP-Za-km-z]{47}", value), "invalid_did")
    try:
        # Reuse the maintained canonical SDK's public Ed25519 decoder; no fetch or vet call.
        require(len(public_key_from_did(value)) == 32, "invalid_did")
    except (ValueError, IndexError, TypeError) as error:
        raise InvalidInput("invalid_did") from error
    return value


def timestamp(value):
    require(
        type(value) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", value),
        "invalid_signed_date",
    )
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidInput("invalid_signed_date") from error


def public_credential(value):
    """Copy exact JSON claims, rejecting custom objects before executing their methods."""
    require(type(value) is dict, "invalid_credential_object")
    ancestors = set()
    nodes = 0

    def copy(item, depth=0):
        nonlocal nodes
        nodes += 1
        require(depth <= 16 and nodes <= 4096, "credential_too_complex")
        kind = type(item)
        if item is None or kind is bool:
            return item
        if kind is str:
            require(len(item) <= MAX_CREDENTIAL_BYTES, "credential_too_large")
            return item
        if kind is int:
            require(abs(item) <= 9007199254740991, "invalid_json_number")
            return item
        if kind is float:
            require(math.isfinite(item), "invalid_json_number")
            return item
        require(kind in (dict, list), "invalid_json_value")
        require(id(item) not in ancestors and len(item) <= 1024, "credential_too_complex")
        ancestors.add(id(item))
        if kind is list:
            result = [copy(child, depth + 1) for child in item]
        else:
            result = {}
            for key, child in item.items():
                require(type(key) is str and len(key) <= 256, "invalid_json_key")
                require(key not in {"__proto__", "prototype", "constructor", "toJSON"}, "invalid_json_key")
                result[key] = copy(child, depth + 1)
        ancestors.remove(id(item))
        return result

    snapshot = copy(value)
    try:
        raw = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, UnicodeError) as error:
        raise InvalidInput("invalid_json_value") from error
    require(len(raw) <= MAX_CREDENTIAL_BYTES, "credential_too_large")
    return snapshot, raw


def passport(value, expected_issuer, expected_subject):
    issuer, subject = did(expected_issuer), did(expected_subject)
    credential, raw = public_credential(value)
    require(credential.get("issuer") == issuer, "issuer_mismatch")
    claims = credential.get("credentialSubject")
    require(type(claims) is dict and claims.get("id") == subject, "subject_mismatch")
    types = credential.get("type")
    require(
        type(types) is list
        and all(type(item) is str for item in types)
        and len(types) == len(set(types))
        and {"VerifiableCredential", "AgentGuildPassport"}.issubset(types),
        "unsupported_credential_type",
    )
    proof = credential.get("proof")
    require(type(proof) is dict, "invalid_proof")
    require(
        proof.get("type") == "DataIntegrityProof"
        and proof.get("cryptosuite") == "eddsa-jcs-2022"
        and proof.get("proofPurpose") == "assertionMethod"
        and proof.get("verificationMethod") == issuer + "#" + issuer[8:],
        "unsupported_proof",
    )
    signature = proof.get("proofValue")
    require(type(signature) is str and re.fullmatch(r"z[1-9A-HJ-NP-Za-km-z]{64,90}", signature), "invalid_proof")
    encoded = signature[1:]
    number = 0
    for char in encoded:
        number = number * 58 + _BASE58.index(char)
    decoded_size = (number.bit_length() + 7) // 8 + len(encoded) - len(encoded.lstrip("1"))
    require(decoded_size == 64, "invalid_proof")
    start, end = timestamp(credential.get("validFrom")), timestamp(credential.get("validUntil"))
    require(start < end, "invalid_signed_date")
    return credential, raw, issuer, subject


def check_time(credential, max_age_seconds):
    now = datetime.now(timezone.utc)
    start, end = timestamp(credential["validFrom"]), timestamp(credential["validUntil"])
    require(start <= now < end, "outside_signed_validity")
    require((now - start).total_seconds() <= max_age_seconds, "stale_passport")
    return now.isoformat()
