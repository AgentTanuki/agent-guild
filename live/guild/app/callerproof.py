"""agent-guild/caller-proof/v1 — cryptographic machine attribution.

A transport-neutral signed caller envelope that an autonomous machine can
CREATE and the Guild (or anyone) can VERIFY offline — no accounts, no human
verification, no trusted user-agent strings, no manual classification. The
caller's existing self-controlled did:key signs a JCS-canonical payload that
binds:

    v            "agent-guild/caller-proof/v1"       (protocol version)
    did          the caller's did:key
    method       the action ("GET", "POST", "tools/call", "message/send")
    resource     the canonical resource (HTTP request-target) or tool name
    body_sha256  sha-256 hex of the exact request body ("" body hashes too)
    iat / exp    issued / expiry unix seconds (bounded lifetime)
    nonce        unique per proof — replay-protected server-side (durable)
    aud          "agent-guild" (intended audience)

Signature: Ed25519 over the JCS canonicalization of the payload (the same
`crypto.sign_jcs` / `verify_jcs` primitives the independent verifiers
already check), hex-encoded. Verification is OFFLINE except the durable
nonce-replay mark.

Transport mappings:
  * HTTP — header ``X-Guild-Caller-Proof: base64(JSON envelope)``; the
    proof binds the actual HTTP method and the exact request-target
    (path + '?' + query as sent) and the raw request body;
  * MCP  — ``_meta["io.agent-guild/caller-proof"] = envelope``; binds
    method="tools/call", resource=<tool name> and
    body_sha256 = sha256(JCS(visible tool arguments, minus api_key/_meta))
    — see :func:`mcp_args_body`;
  * A2A  — message ``metadata["io.agent-guild/caller-proof"] = envelope``;
    binds method="message/send" and resource="/a2a" plus the JCS of the
    message parts. (Documented here; A2A wiring lands with the A2A files.)

Anonymous calls remain allowed — they are simply labelled UNVERIFIED. A
user-agent string is diagnostics only; it can never create verified-external
status.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any, Optional

from . import crypto

PROTOCOL = "agent-guild/caller-proof/v1"
AUDIENCE = "agent-guild"
HTTP_HEADER = "X-Guild-Caller-Proof"
MCP_META_KEY = "io.agent-guild/caller-proof"
A2A_METADATA_KEY = "io.agent-guild/caller-proof"
MAX_TTL_S = 600.0                 # proofs are short-lived by design
MAX_ENVELOPE_BYTES = 8 * 1024


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def mcp_args_body(arguments: dict[str, Any]) -> bytes:
    """The canonical MCP 'body': JCS of the visible tool arguments with
    credentials and metadata excluded (they are transport concerns, not the
    request the caller is attesting to)."""
    visible = {k: v for k, v in (arguments or {}).items()
               if k not in ("api_key", "_meta")}
    return crypto.canonicalize_jcs(visible).encode("utf-8")


def a2a_parts_body(parts: Any) -> bytes:
    """The canonical A2A 'body': JCS of the message PARTS only. The proof
    envelope itself rides message.metadata, which is deliberately excluded —
    a proof must never (circularly) sign itself, and adding/altering
    metadata must not invalidate a proof over the actual content."""
    return crypto.canonicalize_jcs(
        parts if isinstance(parts, list) else []).encode("utf-8")


def create_proof(private_hex: str, did: str, *, method: str, resource: str,
                 body: bytes = b"", ttl_s: float = 300.0,
                 nonce: Optional[str] = None,
                 now: Optional[float] = None) -> dict[str, Any]:
    """Create one caller-proof envelope. Entirely self-serve: any machine
    holding its own did:key private key can call this (or re-implement it —
    the format is JCS + Ed25519, nothing Guild-specific)."""
    now = time.time() if now is None else now
    payload = {
        "v": PROTOCOL,
        "did": did,
        "method": str(method),
        "resource": str(resource),
        "body_sha256": body_sha256(body),
        "iat": int(now),
        "exp": int(now + min(ttl_s, MAX_TTL_S)),
        "nonce": nonce or secrets.token_urlsafe(24),
        "aud": AUDIENCE,
    }
    return {"payload": payload,
            "signature": crypto.sign_jcs(payload, private_hex),
            "verificationMethod": crypto.did_key_verification_method(did)}


def _fail(reason: str) -> dict[str, Any]:
    return {"verified": False, "did": None, "reason": reason}


def verify_proof(store: Any, envelope: Any, *, method: str, resource: str,
                 body: bytes = b"", now: Optional[float] = None,
                 mark_nonce: bool = True) -> dict[str, Any]:
    """Verify one caller-proof envelope against the EXACT request the
    server received. Enforces, in order: shape, protocol version, audience,
    expiry/issued window, signature (offline, did:key), exact request
    binding (method + resource + body hash) and durable nonce replay
    protection. Returns {"verified": bool, "did": str|None, "reason": str}.

    Failure NEVER raises — an invalid proof simply leaves the call
    unverified (anonymous calls are allowed)."""
    now = time.time() if now is None else now
    if not isinstance(envelope, dict):
        return _fail("malformed envelope")
    payload = envelope.get("payload")
    sig = envelope.get("signature")
    if not isinstance(payload, dict) or not isinstance(sig, str):
        return _fail("malformed envelope: payload/signature")
    if len(json.dumps(payload)) > MAX_ENVELOPE_BYTES:
        return _fail("oversized envelope")
    if payload.get("v") != PROTOCOL:
        return _fail(f"unsupported protocol version {payload.get('v')!r}")
    if payload.get("aud") != AUDIENCE:
        return _fail("wrong audience: this proof was not intended for "
                     "agent-guild")
    did = payload.get("did")
    if not (isinstance(did, str) and did.startswith("did:key:")):
        return _fail("missing/unsupported did (did:key required)")
    try:
        iat, exp = int(payload.get("iat")), int(payload.get("exp"))
    except (TypeError, ValueError):
        return _fail("malformed iat/exp")
    if exp <= now:
        return _fail("proof expired")
    if iat >= exp:
        return _fail("iat must be strictly before exp (a proof needs a "
                     "positive, bounded lifetime)")
    if iat > now + 120:
        return _fail("proof issued in the future (clock skew > 120s)")
    if exp - iat > MAX_TTL_S + 120:
        return _fail("expiry window exceeds the allowed lifetime")
    nonce = payload.get("nonce")
    if not (isinstance(nonce, str) and 8 <= len(nonce) <= 128):
        return _fail("missing/malformed nonce")
    # signature BEFORE binding: a forged did or altered payload dies here
    try:
        pub = crypto.public_key_from_did(did)
        if not crypto.verify_jcs(payload, sig, pub):
            return _fail("signature verification failed")
    except Exception:
        return _fail("signature verification failed (unresolvable did)")
    # exact request binding
    if payload.get("method") != str(method):
        return _fail(f"request binding mismatch: method {method!r} != "
                     f"proof {payload.get('method')!r}")
    if payload.get("resource") != str(resource):
        return _fail("request binding mismatch: resource differs")
    if payload.get("body_sha256") != body_sha256(body):
        return _fail("request binding mismatch: body hash differs")
    # durable nonce replay protection (the ONLY non-offline step)
    if mark_nonce:
        key = hashlib.sha256(f"{did}|{nonce}".encode()).hexdigest()
        if not store.caller_proof_nonce_check_and_mark(key, float(exp)):
            return _fail("nonce replay: this proof was already used")
    return {"verified": True, "did": did, "reason": "ok",
            "nonce": nonce, "iat": iat, "exp": exp}


def parse_http_header(value: str) -> Optional[dict[str, Any]]:
    """Decode the HTTP transport form: base64(JSON envelope). Returns None
    on any malformation (the call proceeds unverified)."""
    if not value or len(value) > MAX_ENVELOPE_BYTES * 2:
        return None
    try:
        return json.loads(base64.b64decode(value, validate=True))
    except Exception:
        return None


def http_resource(path: str, query: str = "") -> str:
    """The canonical HTTP resource a proof binds: the exact request-target
    as sent — path plus '?query' when a query string is present."""
    return path + (f"?{query}" if query else "")


def schema_document(base: str = "") -> dict[str, Any]:
    """The machine-readable schema + instructions published at
    /caller-proof and linked from every discovery surface."""
    example_payload = {
        "v": PROTOCOL, "did": "did:key:z6Mk...",
        "method": "GET", "resource": "/check?capability=translation",
        "body_sha256": body_sha256(b""),
        "iat": 1760000000, "exp": 1760000300,
        "nonce": "u3Zk...24-url-safe-bytes", "aud": AUDIENCE,
    }
    return {
        "protocol": PROTOCOL,
        "purpose": ("prove that a request (and any x402 payment made with "
                    "it) came from a specific autonomous machine — no "
                    "accounts, no humans, no trusted user-agent strings"),
        "payload_fields": {
            "v": f"literal {PROTOCOL!r}",
            "did": "the caller's self-controlled did:key",
            "method": "HTTP method, 'tools/call' (MCP) or 'message/send' "
                      "(A2A)",
            "resource": "exact HTTP request-target (path?query) or MCP "
                        "tool name",
            "body_sha256": "sha-256 hex of the exact request body (empty "
                           "body hashes the empty string)",
            "iat": "issued at (unix seconds)",
            "exp": f"expiry (unix seconds; lifetime ≤ {int(MAX_TTL_S)}s)",
            "nonce": "unique per proof — single-use, replay-rejected",
            "aud": f"literal {AUDIENCE!r}",
        },
        "signature": ("Ed25519 over the RFC 8785 (JCS) canonicalization of "
                      "`payload`, hex-encoded, key = the did:key itself"),
        "transports": {
            "http": {"header": HTTP_HEADER,
                     "encoding": "base64(JSON envelope)",
                     "resource": "path?query exactly as sent"},
            "mcp": {"meta_key": MCP_META_KEY,
                    "method": "tools/call", "resource": "<tool name>",
                    "body": "sha256 of JCS(tool arguments minus "
                            "api_key/_meta)"},
            "a2a": {"metadata_key": A2A_METADATA_KEY,
                    "method": "message/send", "resource": "/a2a",
                    "body": "sha256 of JCS(message parts)"},
        },
        "verification": (
            "1. JCS-canonicalize `payload`; 2. verify the Ed25519 signature "
            "against the did:key's public key (multibase b58, "
            "ed25519-pub multicodec 0xed01); 3. check aud, iat/exp, exact "
            "method/resource/body binding; 4. reject reused nonces. "
            "Anonymous calls remain allowed — they are simply UNVERIFIED."),
        "example": {"payload": example_payload,
                    "signature": "<128 hex chars>",
                    "verificationMethod": "did:key:z6Mk...#z6Mk..."},
        "registration": ("self-serve: POST /agents/register with your own "
                         "public_key to hold a self-sovereign did:key; "
                         "creating proofs requires nothing from the Guild"),
    }
