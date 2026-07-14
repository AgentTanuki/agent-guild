"""Authenticated Coinbase CDP facilitator support for the x402 v2 rail.

The CDP facilitator (https://api.cdp.coinbase.com/platform/v2/x402) requires
CDP API keys: every /verify, /settle and /supported request carries an
`Authorization: Bearer <JWT>` where the JWT is generated PER REQUEST, bound
to the exact method+host+path, and expires in ~120 seconds.

This module reproduces the official CDP SDK's JWT exactly (verified against
cdp-sdk `cdp/auth/utils/jwt.py` + `cdp/x402/x402.py` and the CDP
authentication docs, 2026-07-14) without importing cdp-sdk — its pip package
drags in web3/solana/aiohttp, which must not enter the Guild's pinned
production and framework-CI environments. Format:

  header  {"alg": "EdDSA"|"ES256", "kid": <api key id>, "typ": "JWT",
           "nonce": <16 random digits>}
  claims  {"sub": <api key id>, "iss": "cdp", "aud": null,
           "nbf": now, "exp": now + 120,
           "uris": ["POST api.cdp.coinbase.com/platform/v2/x402/verify"]}

Key formats (same parser as the CDP SDK):
  * Ed25519 (recommended): base64 of 64 bytes (32-byte seed + 32-byte pub),
    signed with EdDSA;
  * EC P-256: PEM "EC PRIVATE KEY", signed with ES256 (JOSE raw r||s).

The x402 SDK consumes this through its documented AuthProvider hook:
`FacilitatorConfig(url=..., auth_provider=CreateHeadersAuthProvider(fn))`
where `fn` returns the per-endpoint header dicts — the exact shape
`cdp.x402.create_cdp_auth_headers` produces.

SECRETS: `CDP_API_KEY_ID` / `CDP_API_KEY_SECRET` (the official CDP SDK
variable names) are read from the environment at request time, passed only
into the signer, and never logged, returned, persisted or attached to any
module/global state.
"""
from __future__ import annotations

import base64
import json
import os
import secrets as _secrets
import time
from typing import Any, Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.hashes import SHA256

CDP_FACILITATOR_HOST = "api.cdp.coinbase.com"
CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
_JWT_TTL_SECONDS = 120


def credentials_configured() -> bool:
    """True iff both CDP API key env vars are set (never exposes values)."""
    return bool(os.environ.get("CDP_API_KEY_ID")
                and os.environ.get("CDP_API_KEY_SECRET"))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _parse_private_key(key_data: str):
    """PEM EC (ES256) or base64 Ed25519 (EdDSA) — same acceptance rules as
    the CDP SDK's `_parse_private_key`."""
    if "\\n" in key_data:                      # unquoted env-var newlines
        key_data = key_data.replace("\\n", "\n")
    try:
        key = serialization.load_pem_private_key(key_data.encode(), password=None)
        if isinstance(key, ec.EllipticCurvePrivateKey):
            return key
    except Exception:
        pass
    try:
        decoded = base64.b64decode(key_data)
        if len(decoded) == 64:                 # 32-byte seed + 32-byte public
            return ed25519.Ed25519PrivateKey.from_private_bytes(decoded[:32])
    except Exception:
        pass
    raise ValueError("CDP API key secret must be a PEM EC key or a base64 "
                     "Ed25519 key")           # never echoes the key material


def generate_cdp_jwt(api_key_id: str, api_key_secret: str,
                     request_method: str, request_host: str,
                     request_path: str,
                     expires_in: int = _JWT_TTL_SECONDS) -> str:
    """A CDP Bearer JWT for exactly one (method, host, path) — the format the
    official CDP SDK produces (see module docstring)."""
    private_key = _parse_private_key(api_key_secret)
    if isinstance(private_key, ec.EllipticCurvePrivateKey):
        algorithm = "ES256"
    else:
        algorithm = "EdDSA"
    now = int(time.time())
    header = {"alg": algorithm, "kid": api_key_id, "typ": "JWT",
              "nonce": "".join(_secrets.choice("0123456789") for _ in range(16))}
    claims = {"sub": api_key_id, "iss": "cdp", "aud": None,
              "nbf": now, "exp": now + expires_in,
              "uris": [f"{request_method.upper()} {request_host}{request_path}"]}
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode())
                     + "."
                     + _b64url(json.dumps(claims, separators=(",", ":")).encode()))
    if algorithm == "EdDSA":
        sig = private_key.sign(signing_input.encode("ascii"))
    else:
        der = private_key.sign(signing_input.encode("ascii"),
                               ec.ECDSA(SHA256()))
        r, s = decode_dss_signature(der)       # JOSE wants raw r||s (64 bytes)
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return signing_input + "." + _b64url(sig)


def create_cdp_headers() -> dict[str, dict[str, str]]:
    """Per-endpoint auth headers for the CDP facilitator — the dict shape the
    x402 SDK's CreateHeadersAuthProvider adapts (verify/settle/supported each
    carry a fresh single-use Bearer JWT). Reads the secrets from the
    environment at CALL time and does not retain them."""
    api_key_id = os.environ.get("CDP_API_KEY_ID", "")
    api_key_secret = os.environ.get("CDP_API_KEY_SECRET", "")
    if not (api_key_id and api_key_secret):
        raise RuntimeError(
            "CDP facilitator selected but CDP_API_KEY_ID / CDP_API_KEY_SECRET "
            "are not configured")             # message carries NO key material
    route = "/platform/v2/x402"

    def _hdrs(method: str, endpoint: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + generate_cdp_jwt(
                api_key_id, api_key_secret, method,
                CDP_FACILITATOR_HOST, f"{route}/{endpoint}"),
            "Correlation-Context": "sdk_version=agent-guild,source=x402",
        }

    return {
        "verify": _hdrs("POST", "verify"),
        "settle": _hdrs("POST", "settle"),
        "supported": _hdrs("GET", "supported"),
    }


def auth_provider() -> Any:
    """The x402-SDK AuthProvider for the CDP facilitator."""
    from x402.http import CreateHeadersAuthProvider
    return CreateHeadersAuthProvider(create_cdp_headers)


# typing helper for tests
CreateHeaders = Callable[[], dict[str, dict[str, str]]]
