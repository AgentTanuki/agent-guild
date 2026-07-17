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
# bounded challenge store: outstanding-per-DID and global caps (expired
# challenges are garbage-collected on every new challenge).
MAX_CHALLENGES_PER_DID = 8
MAX_CHALLENGES_GLOBAL = 10_000
# CAIP-2 networks a binding may name: the allowed x402 SETTLEMENT networks
# (attribution is exact (address, network), so a binding on an unknown
# network could never attribute anything anyway — reject it early).
DEFAULT_ALLOWED_NETWORKS = ("eip155:8453", "eip155:84532")


def allowed_networks() -> set[str]:
    import os
    raw = os.environ.get("GUILD_WALLET_BINDING_NETWORKS") or ""
    nets = {n.strip() for n in raw.split(",") if n.strip()}
    return nets or set(DEFAULT_ALLOWED_NETWORKS)


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
    """Free, self-serve challenge. The nonce is single-use and expiring.
    The DID is validated (did:key, resolvable public key) BEFORE anything
    is persisted; expired challenges are garbage-collected; the store is
    bounded per-DID and globally."""
    if not (isinstance(did, str) and did.startswith("did:key:")):
        raise BindingError("did:key required")
    try:
        crypto.public_key_from_did(did)
    except Exception:
        raise BindingError("unresolvable did:key — a challenge is only "
                           "issued for a valid, resolvable DID")
    nonce = "wb_" + secrets.token_urlsafe(24)
    expires_at = _iso_in(CHALLENGE_TTL_S)
    now = _now_iso()
    with store.lock, store._txn():
        # GC: expired challenges never linger
        store.wallet_binding_challenges = {
            n: ch for n, ch in store.wallet_binding_challenges.items()
            if str(ch.get("expires_at")) >= now}
        outstanding = sum(
            1 for ch in store.wallet_binding_challenges.values()
            if ch.get("did") == did)
        if outstanding >= MAX_CHALLENGES_PER_DID:
            raise BindingError(
                f"too many outstanding challenges for this did "
                f"(max {MAX_CHALLENGES_PER_DID}); complete or let one "
                "expire first")
        if len(store.wallet_binding_challenges) >= MAX_CHALLENGES_GLOBAL:
            raise BindingError("challenge store is at capacity; retry "
                               "after outstanding challenges expire")
        store.wallet_binding_challenges[nonce] = {
            "did": did, "expires_at": expires_at,
            "created_at": now}
        if store.backend is not None:
            store._persist_kv("wallet_binding_challenges",
                              store.wallet_binding_challenges)
        store._save()
    return {"protocol": PROTOCOL, "nonce": nonce, "expires_at": expires_at,
            "allowed_networks": sorted(allowed_networks()),
            "binding_template": binding_payload(
                did=did, address="<your 0x address>",
                network="<CAIP-2, one of allowed_networks>",
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
    network = str(binding["network"])
    if network not in allowed_networks():
        raise BindingError(
            "network must be an allowed CAIP-2 settlement network "
            f"({', '.join(sorted(allowed_networks()))})")
    # challenge nonce: must exist, be unexpired and single-use
    nonce = str(binding["nonce"])
    ch = store.wallet_binding_challenges.get(nonce)
    if ch is None:
        raise BindingError("unknown or already-used challenge nonce")
    if ch.get("did") != did:
        raise BindingError("challenge nonce was issued to a different did")
    if str(ch.get("expires_at")) < _now_iso():
        raise BindingError("challenge nonce expired")
    # the signed binding must carry the challenge's expiry EXACTLY — a
    # caller-chosen expiry could outlive the challenge it claims to answer.
    if str(binding.get("expires_at")) != str(ch.get("expires_at")):
        raise BindingError("binding expires_at must equal the challenge "
                           "expiry exactly")
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
    superseded: list[str] = []
    with store.lock, store._txn():
        # DETERMINISTIC rotation/supersession: issuing a credential for an
        # (address, network) pair supersedes EVERY previously-active
        # credential for that exact pair (whatever its DID), so one
        # address/network can never ambiguously represent multiple active
        # DIDs — the newest issuance wins, append-only audited.
        for old in store.wallet_bindings.values():
            if (old.get("address") == address.lower()
                    and str(old.get("network")) == network
                    and old.get("status") == "active"):
                old["status"] = "superseded"
                old["superseded_at"] = _now_iso()
                old["superseded_by"] = cred["credential_id"]
                superseded.append(old["credential_id"])
        store.wallet_bindings[cred["credential_id"]] = cred
        if store.backend is not None:
            store._persist_kv("wallet_bindings", store.wallet_bindings)
        store._save()
    store.record_event(None, "wallet_binding_issued", did=did,
                       credential_id=cred["credential_id"],
                       address=address.lower(), network=network,
                       superseded=superseded or None)
    return dict(cred)


def credential_offline_valid(store: Any, cred: Any) -> bool:
    """OFFLINE cryptographic validity ONLY: the Guild's Ed25519 signature
    over the credential body, plus the embedded validity window. This is
    everything a third party can check with no connection to the Guild.
    It deliberately says NOTHING about revocation or supersession — that is
    live status (`credential_status_live`), a different claim."""
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
    return str(cred.get("expires_at")) >= _now_iso()


def credential_status_live(store: Any, cred: Any) -> bool:
    """LIVE revocation/status check against the Guild's credential store —
    NOT an offline verification (it requires the live store). True iff the
    credential is currently active and unexpired there."""
    if not isinstance(cred, dict):
        return False
    live = store.wallet_bindings.get(cred.get("credential_id") or "")
    return bool(live and live.get("status") == "active"
                and str(live.get("expires_at")) >= _now_iso())


def verify_credential(store: Any, cred: Any) -> bool:
    """Full check = OFFLINE cryptographic validity (signature + window)
    AND LIVE revocation/status. The two are separable on purpose — see
    credential_offline_valid / credential_status_live; only the offline
    half may ever be described as offline verification."""
    return (credential_offline_valid(store, cred)
            and credential_status_live(store, cred))


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
