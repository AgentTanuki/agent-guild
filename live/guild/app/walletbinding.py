"""DID ↔ payment-wallet binding (agent-guild/wallet-binding/v1).

A no-transaction, no-gas dual-signature challenge that binds an agent's
self-controlled did:key to the EVM address it pays with:

  1. the agent asks for a challenge (nonce + expiry) — free, self-serve;
  2. the agent's did:key signs the JCS-canonical binding payload
     {v, did, address, network, aud:"agent-guild", nonce, expires_at};
  3. the EVM address signs the SAME canonical payload with a standard
     recoverable EIP-191 personal_sign signature;
  4. both verify ⇒ the Guild issues a signed, EXPIRING wallet-binding
     credential (verifiable offline against the Guild's public key).

At settlement the x402 payer address is resolved to its active credential —
that is what lets a confirmed mainnet payment be attributed to a specific
machine DID. A self-declared wallet address is NEVER proof. Rotation =
binding a new wallet (plain machine call); revocation = a DID-signed revoke
request; both are append-only audited. No human review, email, company
record or API key is involved anywhere.
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import crypto

PROTOCOL = "agent-guild/wallet-binding/v1"
AUDIENCE = "agent-guild"
CHALLENGE_TTL_S = 900.0
CREDENTIAL_TTL_DAYS = 90


def _iso_in(seconds: float) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(seconds=seconds)).isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def binding_payload(*, did: str, address: str, network: str, nonce: str,
                    expires_at: str) -> dict[str, Any]:
    """The canonical binding BOTH keys sign."""
    return {"v": PROTOCOL, "did": did, "address": address.lower(),
            "network": network, "aud": AUDIENCE, "nonce": nonce,
            "expires_at": expires_at}


def binding_message(binding: dict[str, Any]) -> str:
    """The exact string the EVM key signs (EIP-191 personal_sign text):
    the JCS canonicalization of the binding payload."""
    return crypto.canonicalize_jcs(binding)


def new_challenge(store: Any, did: str) -> dict[str, Any]:
    """Free, self-serve challenge. The nonce is single-use and expiring."""
    nonce = "wb_" + secrets.token_urlsafe(24)
    expires_at = _iso_in(CHALLENGE_TTL_S)
    with store.lock, store._txn():
        store.wallet_binding_challenges[nonce] = {
            "did": did, "expires_at": expires_at,
            "created_at": _now_iso()}
        if store.backend is not None:
            store._persist_kv("wallet_binding_challenges",
                              store.wallet_binding_challenges)
        store._save()
    return {"protocol": PROTOCOL, "nonce": nonce, "expires_at": expires_at,
            "binding_template": binding_payload(
                did=did, address="<your 0x address>",
                network="<CAIP-2, e.g. eip155:8453>",
                nonce=nonce, expires_at=expires_at),
            "sign": {
                "did_signature": "Ed25519 over JCS(binding) by the did:key",
                "evm_signature": ("EIP-191 personal_sign over the JCS "
                                  "string of the SAME binding, hex"),
            }}


class BindingError(ValueError):
    pass


def verify_and_issue(store: Any, binding: Any, did_signature: str,
                     evm_signature: str) -> dict[str, Any]:
    """Verify both signatures over the SAME canonical binding and issue the
    Guild-signed credential. Raises BindingError with a machine-readable
    reason on ANY failure — a self-declared address can never pass."""
    if not isinstance(binding, dict):
        raise BindingError("malformed binding")
    for k in ("v", "did", "address", "network", "aud", "nonce",
              "expires_at"):
        if not binding.get(k):
            raise BindingError(f"binding missing required field {k!r}")
    if binding["v"] != PROTOCOL:
        raise BindingError("unsupported binding protocol version")
    if binding["aud"] != AUDIENCE:
        raise BindingError("wrong audience")
    did = binding["did"]
    if not str(did).startswith("did:key:"):
        raise BindingError("did:key required")
    address = str(binding["address"]).lower()
    if not (address.startswith("0x") and len(address) == 42):
        raise BindingError("malformed EVM address")
    # challenge nonce: must exist, be unexpired and single-use
    nonce = str(binding["nonce"])
    ch = store.wallet_binding_challenges.get(nonce)
    if ch is None:
        raise BindingError("unknown or already-used challenge nonce")
    if ch.get("did") != did:
        raise BindingError("challenge nonce was issued to a different did")
    if str(ch.get("expires_at")) < _now_iso():
        raise BindingError("challenge nonce expired")
    if str(binding.get("expires_at")) < _now_iso():
        raise BindingError("binding already expired")
    # 1. the DID controls this binding
    try:
        if not crypto.verify_jcs(binding, did_signature,
                                 crypto.public_key_from_did(did)):
            raise BindingError("did signature invalid")
    except BindingError:
        raise
    except Exception:
        raise BindingError("did signature invalid (unresolvable did)")
    # 2. the WALLET controls this binding — standard recoverable EIP-191
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        recovered = Account.recover_message(
            encode_defunct(text=binding_message(binding)),
            signature=bytes.fromhex(str(evm_signature).removeprefix("0x")))
    except Exception as e:
        raise BindingError(f"evm signature invalid: {type(e).__name__}")
    if recovered.lower() != address:
        raise BindingError("evm signature recovers a different address — "
                           "a self-declared wallet is never proof")
    # both proven — consume the nonce and issue the credential
    with store.lock, store._txn():
        store.wallet_binding_challenges.pop(nonce, None)
        if store.backend is not None:
            store._persist_kv("wallet_binding_challenges",
                              store.wallet_binding_challenges)
        store._save()
    return issue_credential(store, did=did, address=address,
                            network=str(binding["network"]),
                            challenge_nonce=nonce)


def issue_credential(store: Any, *, did: str, address: str, network: str,
                     challenge_nonce: str) -> dict[str, Any]:
    """Persist + return the Guild-signed, expiring wallet-binding
    credential (called ONLY after both signatures verified, or from tests
    that construct the post-verification state directly)."""
    gid = store.guild_identity()
    cred = {
        "type": "AgentGuildWalletBinding",
        "protocol": PROTOCOL,
        "credential_id": "wbc_" + secrets.token_hex(12),
        "did": did,
        "address": address.lower(),
        "network": network,
        "issued_at": _now_iso(),
        "expires_at": _iso_in(CREDENTIAL_TTL_DAYS * 86400),
        "issuer": gid["did"],
        "challenge_nonce": challenge_nonce,
        "status": "active",
    }
    cred["proof"] = crypto.sign_jcs(
        {k: v for k, v in cred.items() if k != "proof"},
        gid["private_key"])
    with store.lock, store._txn():
        store.wallet_bindings[cred["credential_id"]] = cred
        if store.backend is not None:
            store._persist_kv("wallet_bindings", store.wallet_bindings)
        store._save()
    store.record_event(None, "wallet_binding_issued", did=did,
                       credential_id=cred["credential_id"],
                       address=address.lower(), network=network)
    return dict(cred)


def verify_credential(store: Any, cred: Any) -> bool:
    """Offline credential check: Guild signature + expiry + live status."""
    if not isinstance(cred, dict):
        return False
    gid = store.guild_identity()
    body = {k: v for k, v in cred.items() if k != "proof"}
    try:
        if not crypto.verify_jcs(body, cred.get("proof") or "",
                                 gid["public_key"]):
            return False
    except Exception:
        return False
    if str(cred.get("expires_at")) < _now_iso():
        return False
    live = store.wallet_bindings.get(cred.get("credential_id") or "")
    return bool(live and live.get("status") == "active"
                and str(live.get("expires_at")) >= _now_iso())


def revoke(store: Any, request: Any, did_signature: str) -> dict[str, Any]:
    """Machine-executable revocation: the request {action:'revoke',
    credential_id, did} must be signed by the credential's own DID.
    Append-only: the credential record flips to revoked and an audit event
    is recorded; nothing is deleted."""
    if not isinstance(request, dict) or request.get("action") != "revoke":
        raise BindingError("malformed revoke request")
    cred_id = str(request.get("credential_id") or "")
    cred = store.wallet_bindings.get(cred_id)
    if cred is None:
        raise BindingError("unknown credential")
    did = cred["did"]
    if request.get("did") != did:
        raise BindingError("revoke request names a different did")
    try:
        if not crypto.verify_jcs(request, did_signature,
                                 crypto.public_key_from_did(did)):
            raise BindingError("revoke signature invalid")
    except BindingError:
        raise
    except Exception:
        raise BindingError("revoke signature invalid")
    with store.lock, store._txn():
        cred["status"] = "revoked"
        cred["revoked_at"] = _now_iso()
        if store.backend is not None:
            store._persist_kv("wallet_bindings", store.wallet_bindings)
        store._save()
    store.record_event(None, "wallet_binding_revoked", did=did,
                       credential_id=cred_id)
    return {"credential_id": cred_id, "status": "revoked"}
