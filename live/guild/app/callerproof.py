"""Agent Guild caller proofs — cryptographic machine attribution.

A transport-neutral signed caller envelope that an autonomous machine can
CREATE and the Guild (or anyone) can VERIFY offline — no accounts, no human
verification, no trusted user-agent strings, no manual classification. The
caller's existing self-controlled identity key signs a JCS-canonical payload
that binds:

    v            caller-proof protocol version
    did          the caller's did:key OR did:pkh EVM wallet identity
    method       the action ("GET", "POST", "tools/call", "message/send")
    resource     the canonical resource (HTTP request-target) or tool name
    body_sha256  sha-256 hex of the exact request body ("" body hashes too)
    iat / exp    issued / expiry unix seconds (bounded lifetime)
    nonce        unique per proof — replay-protected server-side (durable)
    aud          "agent-guild" (intended audience)

Signature: either Ed25519 over JCS (``agent-guild/caller-proof/v1``) or an
EIP-191 personal signature over the exact JCS bytes
(``agent-guild/caller-proof-evm/v1``).  The EVM form is deliberately EOA-only
and uses ``did:pkh:eip155:8453:<address>`` so one caller-controlled Base wallet
can authenticate the exact request and pay its x402 challenge. Verification is
OFFLINE except the durable nonce-replay mark.

Transport mappings:
  * HTTP — header ``X-Guild-Caller-Proof: base64(JSON envelope)``; the
    proof binds the actual HTTP method and the exact request-target
    (path + '?' + query as sent) and the raw request body;
  * HTTP marketplace body — ``{"request": {...}, "caller_proof": {...}}``;
    the proof binds the actual method/request-target and
    ``JCS(request)``.  This avoids a circular signature while allowing
    marketplaces that can forward JSON bodies but not custom headers;
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
EVM_PROTOCOL = "agent-guild/caller-proof-evm/v1"
SUPPORTED_PROTOCOLS = (PROTOCOL, EVM_PROTOCOL)
AUDIENCE = "agent-guild"
BASE_MAINNET_CHAIN_ID = 8453
HTTP_HEADER = "X-Guild-Caller-Proof"
HTTP_BODY_REQUEST_KEY = "request"
HTTP_BODY_PROOF_KEY = "caller_proof"
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


def http_marketplace_body(request_body: Any) -> bytes:
    """Canonical bytes signed by the headerless HTTP marketplace transport.

    The proof rides beside ``request`` in the outer JSON object, so it cannot
    sign the raw outer body without signing itself.  Binding RFC 8785 JCS of
    the semantic request gives deterministic, serialization-independent
    verification while every result-affecting field remains signed.
    """
    return crypto.canonicalize_jcs(request_body).encode("utf-8")


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


def _evm_did_parts(did: Any) -> Optional[tuple[int, str]]:
    """Parse the offline-verifiable EOA identity accepted by the EVM proof.

    The chain id is identity domain separation, not a statement about a token
    transfer.  This first version is intentionally Base-mainnet-only because
    that is the network on which the paired x402 payment is offered.
    """
    if not isinstance(did, str):
        return None
    parts = did.split(":")
    if len(parts) != 5 or parts[:3] != ["did", "pkh", "eip155"]:
        return None
    try:
        chain_id = int(parts[3])
    except (TypeError, ValueError):
        return None
    address = parts[4]
    if chain_id != BASE_MAINNET_CHAIN_ID:
        return None
    if (len(address) != 42 or not address.startswith("0x")
            or any(c not in "0123456789abcdefABCDEF" for c in address[2:])):
        return None
    return chain_id, address.lower()


def evm_did(address: str, chain_id: int = BASE_MAINNET_CHAIN_ID) -> str:
    """Canonical did:pkh identity for a Base EOA."""
    did = f"did:pkh:eip155:{int(chain_id)}:{str(address).lower()}"
    if _evm_did_parts(did) is None:
        raise ValueError("a 20-byte Base-mainnet EVM address is required")
    return did


def evm_address_for_did(did: str) -> Optional[str]:
    parts = _evm_did_parts(did)
    return parts[1] if parts is not None else None


def authentication_protocol_for_did(did: str) -> Optional[str]:
    if isinstance(did, str) and did.startswith("did:key:"):
        return PROTOCOL
    if _evm_did_parts(did) is not None:
        return EVM_PROTOCOL
    return None


def supported_sender_did(did: str) -> bool:
    return authentication_protocol_for_did(did) is not None


def create_evm_proof(private_key: str, *, method: str, resource: str,
                     body: bytes = b"", ttl_s: float = 300.0,
                     nonce: Optional[str] = None,
                     now: Optional[float] = None,
                     chain_id: int = BASE_MAINNET_CHAIN_ID) -> dict[str, Any]:
    """Create an EIP-191 proof with the same EOA that can pay x402.

    This helper is primarily a conformance/reference implementation.  A buyer
    should normally use ``createEvmMachineEnvelopeClient({evmSigner})`` so its
    private key remains inside its existing wallet signer.
    """
    from eth_account import Account
    from eth_account.messages import encode_defunct

    account = Account.from_key(private_key)
    did = evm_did(account.address, chain_id)
    now = time.time() if now is None else now
    payload = {
        "v": EVM_PROTOCOL,
        "did": did,
        "method": str(method),
        "resource": str(resource),
        "body_sha256": body_sha256(body),
        "iat": int(now),
        "exp": int(now + min(ttl_s, MAX_TTL_S)),
        "nonce": nonce or secrets.token_urlsafe(24),
        "aud": AUDIENCE,
    }
    message = encode_defunct(
        primitive=crypto.canonicalize_jcs(payload).encode("utf-8"))
    signature = Account.sign_message(message, private_key).signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature
    return {
        "payload": payload,
        "signature": signature,
        "verificationMethod": did + "#blockchainAccountId",
    }


def _fail(reason: str) -> dict[str, Any]:
    return {"verified": False, "did": None, "reason": reason}


def verify_proof(store: Any, envelope: Any, *, method: str, resource: str,
                 body: bytes = b"", now: Optional[float] = None,
                 mark_nonce: bool = True) -> dict[str, Any]:
    """Verify one caller-proof envelope against the EXACT request the
    server received. Enforces, in order: shape, protocol version, audience,
    expiry/issued window, signature (offline, did:key or Base EOA), exact request
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
    protocol = payload.get("v")
    if protocol not in SUPPORTED_PROTOCOLS:
        return _fail(f"unsupported protocol version {payload.get('v')!r}")
    if payload.get("aud") != AUDIENCE:
        return _fail("wrong audience: this proof was not intended for "
                     "agent-guild")
    did = payload.get("did")
    if not supported_sender_did(did):
        return _fail(
            "missing/unsupported did (did:key or Base did:pkh EOA required)")
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
    # signature BEFORE binding: a forged DID or altered payload dies here
    try:
        if protocol == PROTOCOL:
            pub = crypto.public_key_from_did(did)
            if not crypto.verify_jcs(payload, sig, pub):
                return _fail("signature verification failed")
        else:
            from eth_account import Account
            from eth_account.messages import encode_defunct
            parts = _evm_did_parts(did)
            if parts is None:
                return _fail("signature verification failed (malformed EVM DID)")
            _chain_id, expected_address = parts
            canonical = crypto.canonicalize_jcs(payload).encode("utf-8")
            recovered = Account.recover_message(
                encode_defunct(primitive=canonical), signature=sig)
            if str(recovered).lower() != expected_address:
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
            "protocol": protocol,
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
        "protocols": list(SUPPORTED_PROTOCOLS),
        "purpose": ("prove that a request (and any x402 payment made with "
                    "it) came from a specific autonomous machine — no "
                    "accounts, no humans, no trusted user-agent strings"),
        "payload_fields": {
            "v": "one of " + ", ".join(repr(p) for p in SUPPORTED_PROTOCOLS),
            "did": ("the caller's self-controlled did:key, or Base EOA as "
                    "did:pkh:eip155:8453:<address>"),
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
        "signatures": {
            PROTOCOL: ("Ed25519 over RFC 8785 (JCS) payload, hex-encoded; "
                       "public key comes from did:key"),
            EVM_PROTOCOL: ("EIP-191 personal signature over the exact UTF-8 "
                           "RFC 8785 (JCS) payload bytes; recover the EOA and "
                           "match did:pkh address; Base mainnet EOA only"),
        },
        # Backward-compatible scalar for consumers that learned v1 before the
        # additive EVM protocol existed. New consumers should read signatures.
        "signature": ("Ed25519 over the RFC 8785 (JCS) canonicalization of "
                      "`payload`, hex-encoded, key = the did:key itself"),
        "transports": {
            "http": {"header": HTTP_HEADER,
                     "encoding": "base64(JSON envelope)",
                     "resource": "path?query exactly as sent"},
            "http_marketplace_body": {
                "shape": {
                    HTTP_BODY_REQUEST_KEY: "<semantic request object>",
                    HTTP_BODY_PROOF_KEY: "<caller-proof envelope>",
                },
                "method": "POST",
                "resource": "path?query exactly as sent",
                "body": "sha256 of RFC 8785 JCS(request)",
                "strict_outer_keys": True,
                "purpose": ("for relays that forward JSON input but cannot "
                            "forward a custom proof header"),
            },
            "mcp": {"meta_key": MCP_META_KEY,
                    "method": "tools/call", "resource": "<tool name>",
                    "body": "sha256 of JCS(tool arguments minus "
                            "api_key/_meta)"},
            "a2a": {"metadata_key": A2A_METADATA_KEY,
                    "method": "message/send", "resource": "/a2a",
                    "body": "sha256 of JCS(message parts)"},
        },
        "verification": (
            "1. JCS-canonicalize `payload`; 2. verify Ed25519 for did:key OR "
            "recover the EIP-191 EOA for Base did:pkh; 3. check aud, iat/exp, exact "
            "method/resource/body binding; 4. reject reused nonces. "
            "Anonymous calls remain allowed — they are simply UNVERIFIED."),
        "example": {"payload": example_payload,
                    "signature": "<128 hex chars>",
                    "verificationMethod": "did:key:z6Mk...#z6Mk..."},
        "registration": ("No registration is required. Use an existing "
                         "did:key, or let createEvmMachineEnvelopeClient "
                         "authenticate and pay from one caller-owned Base EOA."),
    }
