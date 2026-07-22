#!/usr/bin/env python3
"""Autonomous first-payment canary — secret-silent, one-shot, dry-run by default.

The smallest possible PROOF that an unaffiliated machine can pay Agent Guild
for a trust decision on Base MAINNET with no account and no human, and that the
whole loop is verifiable end-to-end. It is a FIRST-PARTY canary: the buyer is
our own wallet, so a successful settlement is `first_party_mainnet_canary`
evidence, NEVER external adoption or customer revenue.

Design invariants (each enforced below):
  * OFFICIAL client only. The payment is built and signed with the official
    x402 SDK (`x402[evm]` — ExactEvmScheme + EthAccountSigner); no Guild code
    touches the payment path.
  * DRY-RUN by default. Execution requires `--execute` AND stays under a hard
    0.01 USDC lifetime cap; the cap is checked against the quoted amount before
    a signature is ever produced.
  * PAY BEFORE STATE IS IMPOSSIBLE. The payment identifier and the signed
    payload are persisted to the state file BEFORE the paid request is sent, so
    a crash or restart re-sends the IDENTICAL payload → the server's
    payment-identifier idempotency returns the cached result with NO second
    settlement. The canary can never pay twice.
  * DISCOVERY, not a hard-coded route. The paid resource is discovered from the
    published machine surfaces (`/.well-known/agent-guild.json` →
    `machine_payments`, then the live 402 challenge), with a hard-coded
    fallback only if discovery fails.
  * REFUSE ANYTHING UNEXPECTED. Before signing, the canary verifies readiness,
    Base chain id (8453), the canonical USDC contract, the exact price, and the
    pinned treasury, and refuses any unexpected recipient, network, asset,
    amount, resource, or production SHA.
  * INDEPENDENT confirmation. After settlement it verifies the returned result,
    the PAYMENT-RESPONSE, the signed offer, the signed receipt, the Base
    transaction receipt and the exact USDC Transfer, and that
    `/billing/revenue.real_settlement.transactions` rose by exactly one and
    contains the tx hash.

The key is supplied locally (a gitignored `--key-file` or CANARY_PRIVATE_KEY
env) and is NEVER printed, logged or written to the evidence artifact. The
artifact contains ONLY public/non-secret evidence.

The buyer key lives at `live/secrets/x402_mainnet_canary.key` (gitignored via
live/secrets/, mode 0600, bare 0x-hex — the format load_private_key accepts).

    Secret-silent preflight (no signing, no payment, no secret output —
    validates the key file's existence/permissions/format, derives ONLY the
    public address, and checks live readiness):
        python first_party_canary.py --preflight

    Dry run (safe, no signing, no funds moved — the default):
        python first_party_canary.py --dry-run

    After Ramp funds the buyer wallet, the single execution command:
        python first_party_canary.py --execute --watch \
            --key-file live/secrets/x402_mainnet_canary.key

Do NOT run --execute during development. A clean dry run is NOT a payment.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from typing import Any, Optional

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "live" / "guild"))

DEFAULT_BASE = "https://agent-guild-5d5r.onrender.com"
# The protected buyer key (gitignored directory; never read by tooling that
# could print it). --key-file overrides; CANARY_PRIVATE_KEY env wins.
DEFAULT_KEY_FILE = REPO / "live" / "secrets" / "x402_mainnet_canary.key"
# First-party tagging token (gitignored, 0600) — same zero-setup pattern.
DEFAULT_FP_TOKEN_FILE = REPO / "live" / "secrets" / "first_party_token"
LIFETIME_CAP_USDC = 0.01                     # hard ceiling, ALL runs, forever
MAINNET = "eip155:8453"
BASE_CHAIN_ID = 8453
CANARY_CAPABILITY = "code-review"            # one useful trust decision
STATE_VERSION = 1


class Refuse(Exception):
    """A safety precondition failed — refuse to sign/pay, fail closed."""


def _log(msg: str) -> None:
    print(msg, flush=True)


def _first_party_headers() -> dict[str, str]:
    """Default headers tagging every canary request as Guild first-party
    traffic. Token source: GUILD_FIRST_PARTY_TOKEN env, else the gitignored
    DEFAULT_FP_TOKEN_FILE (same zero-setup pattern as the buyer key). The
    token value is NEVER logged. Without it, a settled canary payment is
    honestly — but uselessly — classified `unverified_payer` (the
    2026-07-21 mislabel): the server cannot know our own wallet without
    either this token or the GUILD_X402_FIRST_PARTY_PAYERS allowlist."""
    token = os.environ.get("GUILD_FIRST_PARTY_TOKEN", "").strip()
    if not token:
        try:
            token = DEFAULT_FP_TOKEN_FILE.read_text().strip()
        except OSError:
            token = ""
    return {"X-Agent-Guild-First-Party": token} if token else {}


# --------------------------------------------------------------------------
# credential loading (secret-silent)
# --------------------------------------------------------------------------

def load_private_key(key_file: Optional[str]) -> Optional[str]:
    """Load the buyer key from env or a local key file. NEVER printed. The
    file may be a bare hex string or JSON {"privateKey"|"private_key": ...}."""
    env = os.environ.get("CANARY_PRIVATE_KEY", "").strip()
    if env:
        return env
    if not key_file:
        return None
    try:
        raw = pathlib.Path(key_file).expanduser().read_text().strip()
    except OSError as e:
        raise Refuse(f"key file unreadable: {type(e).__name__}")
    if raw.startswith("{"):
        data = json.loads(raw)
        raw = (data.get("privateKey") or data.get("private_key")
               or data.get("key") or "").strip()
    if not raw:
        raise Refuse("key file contained no private key")
    return raw if raw.startswith("0x") else "0x" + raw


def key_file_facts(key_file: str) -> dict[str, Any]:
    """SECRET-SILENT facts about the key file: existence, permissions and
    whether the format is one load_private_key accepts. The key material is
    validated in-process and never returned, printed or logged."""
    p = pathlib.Path(key_file).expanduser()
    facts: dict[str, Any] = {"path": str(p), "exists": p.exists()}
    if not facts["exists"]:
        return facts
    mode = p.stat().st_mode & 0o777
    facts["permissions_octal"] = oct(mode)
    facts["permissions_private"] = (mode & 0o077) == 0    # no group/other bits
    fmt = "unrecognised"
    try:
        raw = p.read_text().strip()
        if raw.startswith("{"):
            data = json.loads(raw)
            body = (data.get("privateKey") or data.get("private_key")
                    or data.get("key") or "").strip().removeprefix("0x")
            if len(body) == 64 and all(c in "0123456789abcdefABCDEF"
                                       for c in body):
                fmt = "json_private_key"
        else:
            body = raw.removeprefix("0x")
            if len(body) == 64 and all(c in "0123456789abcdefABCDEF"
                                       for c in body):
                fmt = "hex"
    except Exception:
        fmt = "unreadable"
    facts["format"] = fmt
    facts["format_accepted"] = fmt in ("hex", "json_private_key")
    return facts


def derive_public_address(key_file: Optional[str]) -> Optional[str]:
    """Derive ONLY the public EVM address from the buyer key — no signature
    is ever produced. Returns None (with no error output) when the signing
    library is unavailable."""
    key = load_private_key(key_file)
    if not key:
        return None
    try:
        from eth_account import Account
    except ImportError:
        return None
    return Account.from_key(key).address


# --------------------------------------------------------------------------
# discovery + safety verification (read-only, no secrets)
# --------------------------------------------------------------------------

def discover_resource(http, base: str) -> str:
    """Discover the paid resource path from the published machine surface,
    with a hard-coded fallback if discovery fails."""
    try:
        manifest = http.get(base + "/.well-known/agent-guild.json",
                            timeout=20).json()
        ex = (manifest.get("machine_payments") or {}).get(
            "example_paid_resource") or {}
        path = ex.get("path")
        if path == "/check":
            return f"{base}/check?capability={CANARY_CAPABILITY}"
    except Exception as e:  # noqa: BLE001
        _log(f"  discovery via manifest failed ({type(e).__name__}); "
             "falling back to the canonical /check resource")
    return f"{base}/check?capability={CANARY_CAPABILITY}"


def production_sha(http, base: str) -> str:
    """The SHA production is actually serving, from GET /release.

    The endpoint's field is `git_sha` (app/main.py::release) — NOT `sha`.
    Reading the wrong key here silently produced `production_sha: ""` in the
    2026-07-21 settlement evidence (found in the post-ship corrective pass),
    which is why tests/test_first_party_canary.py now pins this reader against
    the REAL FastAPI /release response, never a hand-mocked schema. `unknown`
    (Render env absent) is normalized to "" — unverifiable, never a value."""
    try:
        sha = (http.get(base + "/release", timeout=20).json()
               .get("git_sha") or "").strip()
        return "" if sha == "unknown" else sha
    except Exception:
        return ""


def verify_preconditions(http, base: str, expected_sha: Optional[str]
                         ) -> dict[str, Any]:
    """Verify readiness, chain id, USDC contract, price and pinned treasury.
    Returns the non-secret facts; raises Refuse on anything unexpected."""
    from app import x402, x402_confirm

    readiness = http.get(base + "/x402/readiness", timeout=20).json()
    if not readiness.get("enabled"):
        raise Refuse("x402 rail is not enabled on the target")
    if not readiness.get("config_valid"):
        raise Refuse(f"x402 config invalid: {readiness.get('config_errors')}")
    if readiness.get("network") != MAINNET:
        raise Refuse(f"network is {readiness.get('network')}, expected {MAINNET}")
    if not readiness.get("mainnet"):
        raise Refuse("readiness does not report mainnet")
    recipient = readiness.get("recipient")
    if not recipient or recipient.lower() != x402.MAINNET_TREASURY.lower():
        raise Refuse(f"recipient {recipient} is not the pinned treasury "
                     f"{x402.MAINNET_TREASURY}")
    if not readiness.get("recipient_is_pinned_treasury"):
        raise Refuse("readiness does not confirm the pinned treasury")
    expected_usdc = x402.USDC_BY_NETWORK[MAINNET]
    if (readiness.get("asset") or "").lower() != expected_usdc.lower():
        raise Refuse(f"asset {readiness.get('asset')} is not Base USDC "
                     f"{expected_usdc}")
    if not readiness.get("facilitator_authenticated"):
        raise Refuse("facilitator is not the authenticated CDP facilitator")

    # independent chain identity check (public RPC read)
    rpc = x402_confirm.rpc_url()
    cid = http.post(rpc, json={"jsonrpc": "2.0", "id": 1,
                               "method": "eth_chainId", "params": []},
                    timeout=20).json().get("result")
    if cid is None or int(cid, 16) != BASE_CHAIN_ID:
        raise Refuse(f"RPC chain id is {cid}, expected Base mainnet "
                     f"{BASE_CHAIN_ID}")

    if expected_sha:
        sha = production_sha(http, base)
        if not sha:
            # fail CLOSED: an explicit SHA expectation that cannot be verified
            # is a refusal, not a silent pass (corrective pass 2026-07-22 —
            # the old reader returned "" on every call and this branch never
            # fired, so --expect-sha was a no-op in practice).
            raise Refuse("production SHA is unverifiable (/release returned "
                         f"no usable git_sha) but --expect-sha={expected_sha} "
                         "was demanded")
        if sha != expected_sha:
            raise Refuse(f"production SHA {sha} != expected {expected_sha}")

    return {
        "network": MAINNET,
        "asset": expected_usdc,
        "recipient": recipient,
        "facilitator_host": readiness.get("facilitator_host"),
        "chain_id": BASE_CHAIN_ID,
        "production_sha": production_sha(http, base),
    }


def verify_challenge(challenge: dict[str, Any], requested_url: str,
                     facts: dict[str, Any],
                     http: Any = None) -> dict[str, Any]:
    """Re-check the 402 challenge against the verified facts and the lifetime
    cap. The server's quoted `resource.url` is CANONICAL (it applies default
    query values), so the check confirms the quote is the canonical form of
    the request we made — same trusted origin, same /check path, same
    capability — rather than a byte-for-byte match. Returns (accepted
    requirement, canonical resource url). Refuses any mismatch."""
    from app import x402

    if challenge.get("x402Version") != 2:
        raise Refuse(f"challenge x402Version={challenge.get('x402Version')}")
    quoted = (challenge.get("resource") or {}).get("url") or ""
    host = x402.public_host()
    cap = f"capability={CANARY_CAPABILITY}"
    if not (quoted.startswith(host + "/check") and cap in quoted):
        raise Refuse(f"challenge resource {quoted!r} is not the canonical form "
                     f"of {requested_url!r} on the trusted origin")
    accepts = challenge.get("accepts") or []
    if not accepts:
        raise Refuse("challenge carries no accepts[]")
    req = accepts[0]
    if req.get("network") != MAINNET:
        raise Refuse(f"quoted network {req.get('network')} != {MAINNET}")
    if (req.get("asset") or "").lower() != facts["asset"].lower():
        raise Refuse(f"quoted asset {req.get('asset')} != {facts['asset']}")
    if (req.get("payTo") or "").lower() != facts["recipient"].lower():
        raise Refuse(f"quoted payTo {req.get('payTo')} != pinned treasury")
    amount_atomic = int(req.get("amount") or "0")
    amount_usdc = amount_atomic / 1e6
    if amount_usdc <= 0:
        raise Refuse("quoted amount is zero")
    if amount_usdc > LIFETIME_CAP_USDC:
        raise Refuse(f"quoted amount ${amount_usdc:.6f} exceeds the hard "
                     f"lifetime cap ${LIFETIME_CAP_USDC:.6f}")
    # signed offer must be present and verify against the Guild's did:web
    # SERVICE key (resolved from the trusted origin's DID document),
    # bound to the CANONICAL quoted resource
    offer_ok = _verify_signed_offer(challenge, quoted, req, http=http)
    if not offer_ok:
        raise Refuse("signed offer missing or does not verify")
    return req, quoted


def _resolve_kid_public_key(kid: str, http: Any) -> Optional[str]:
    """Resolve the offer/receipt kid to a public key hex — did:web profile:
      1. the kid's DID must be the did:web of the TRUSTED configured origin
         (a hostile-but-valid did:web signer proves nothing about this
         resource);
      2. the origin's DID document (/.well-known/did.json, fetched over TLS
         from that same trusted origin) must authorise exactly this kid;
      3. the key comes from the document's publicKeyMultibase.
    Legacy did:key kids remain resolvable (self-describing)."""
    from app import x402, x402_artifacts as artifacts
    from app.crypto import public_key_from_did
    did = kid.split("#")[0]
    if did.startswith("did:key:"):
        try:
            return public_key_from_did(did)
        except Exception:
            return None
    if not did.startswith("did:web:"):
        return None
    if not artifacts.kid_matches_origin(kid, x402.public_host()):
        return None                       # hostile origin — refuse
    if http is None:
        return None
    try:
        doc = http.get(x402.public_host() + "/.well-known/did.json",
                       timeout=20).json()
        for method in doc.get("verificationMethod") or []:
            if method.get("id") == kid and method.get("publicKeyMultibase"):
                return public_key_from_did(method["publicKeyMultibase"])
    except Exception:
        return None
    return None


def _verify_signed_offer(challenge: dict[str, Any], resource_url: str,
                         req: dict[str, Any], http: Any = None) -> bool:
    from app import x402_artifacts as artifacts
    exts = challenge.get("extensions") or {}
    oro = exts.get("offer-receipt") or {}
    offers = (oro.get("info") or {}).get("offers") or []
    if not offers:
        return False
    jws = offers[0].get("signature")
    header = artifacts.jws_header(jws)
    kid = header.get("kid", "")
    pub = _resolve_kid_public_key(kid, http)
    if not pub:
        return False
    payload = artifacts.jws_verify(jws, pub)
    return bool(payload and payload.get("resourceUrl") == resource_url
                and payload.get("amount") == req.get("amount"))


# --------------------------------------------------------------------------
# persisted one-shot state (pay-before-state is impossible)
# --------------------------------------------------------------------------

def _load_state(path: pathlib.Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text())
    return {"version": STATE_VERSION, "status": "new"}


def _save_state(path: pathlib.Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)


def _verify_revenue_transition(rs_before: dict[str, Any],
                               rs_after: dict[str, Any], tx: str,
                               *, replayed_signed_payload: bool) -> None:
    """Require one new settlement on the first send, while allowing an
    idempotent recovery to observe either the just-created settlement or the
    already-cached one.  In every case the exact transaction must be present
    and the ledger may never jump by more than one."""
    before = int(rs_before.get("transactions", 0))
    after = int(rs_after.get("transactions", 0))
    delta = after - before
    allowed = {0, 1} if replayed_signed_payload else {1}
    if delta not in allowed:
        expected = "zero or one" if replayed_signed_payload else "exactly one"
        raise Refuse(f"real_settlement.transactions did not increase by "
                     f"{expected}")
    if tx not in (rs_after.get("transaction_hashes") or []):
        raise Refuse("the settled tx hash is not in real_settlement")


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

def run(args) -> int:
    import httpx
    from app import x402, x402_confirm
    from app import x402_artifacts as artifacts

    base = args.base.rstrip("/")
    state_path = pathlib.Path(args.state).expanduser()
    evidence_path = pathlib.Path(args.evidence).expanduser()
    fp_headers = _first_party_headers()
    if args.execute and not fp_headers and not getattr(
            args, "allow_untagged", False):
        raise Refuse(
            "refusing --execute without first-party tagging: set "
            "GUILD_FIRST_PARTY_TOKEN so the settlement classifies as "
            "verified_first_party_canary (or pass --allow-untagged to "
            "knowingly produce an unverified_payer settlement)")
    http = httpx.Client(follow_redirects=True, headers=fp_headers)

    _log(f"Agent Guild first-party mainnet canary — target {base}")
    _log(f"  mode: {'EXECUTE' if args.execute else 'DRY-RUN'}   "
         f"lifetime cap: {LIFETIME_CAP_USDC} USDC")
    _log("  first-party tagging: "
         + ("ON (X-Agent-Guild-First-Party sent; token value never logged)"
            if fp_headers else "OFF — settlement would classify as "
                               "unverified_payer"))

    # 1. discover the paid resource + capture the production SHA
    resource_url = discover_resource(http, base)
    _log(f"  discovered paid resource: {resource_url}")
    expected_sha = args.expect_sha or production_sha(http, base)

    # 2. verify all preconditions (read-only). Refuse anything unexpected.
    facts = verify_preconditions(http, base, expected_sha)
    _log(f"  preconditions OK — network={facts['network']} "
         f"asset={facts['asset']} recipient={facts['recipient']} "
         f"chain_id={facts['chain_id']}")

    # 3. fetch the live 402 challenge and verify it, incl. the signed offer +
    #    the hard price cap.
    r0 = http.get(resource_url, timeout=30)
    if r0.status_code != 402:
        raise Refuse(f"expected 402 from the paid resource, got {r0.status_code}")
    challenge = json.loads(_b64(r0.headers[x402.PAYMENT_REQUIRED_HEADER]))
    req, canonical_url = verify_challenge(challenge, resource_url, facts,
                                          http=http)
    # from here the CANONICAL server-quoted resource is what we pay for
    resource_url = canonical_url
    amount_atomic = req["amount"]
    _log(f"  402 challenge verified — quoted {int(amount_atomic)/1e6:.6f} USDC "
         "(within cap), signed offer verifies")

    # 4. capture the starting revenue state (public)
    revenue_before = http.get(base + "/billing/revenue", timeout=20).json()
    rs_before = revenue_before.get("real_settlement", {})
    _log(f"  revenue before: real_settlement.transactions="
         f"{rs_before.get('transactions')}")

    evidence: dict[str, Any] = {
        "label": "first_party_mainnet_canary",
        "disclaimer": ("First-party canary. A successful settlement is proof "
                       "the machine-payment loop works end-to-end; it is NOT "
                       "external adoption and NOT customer revenue."),
        "target": base,
        "production_sha": facts["production_sha"],
        "resource": resource_url,
        "request_hash": None,
        "network": facts["network"],
        "asset": facts["asset"],
        "recipient": facts["recipient"],
        "amount_atomic": amount_atomic,
        "amount_usdc": int(amount_atomic) / 1e6,
        "lifetime_cap_usdc": LIFETIME_CAP_USDC,
        "first_party_tagged": bool(fp_headers),
        "revenue_before": rs_before,
        "mode": "execute" if args.execute else "dry_run",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    if not args.execute:
        evidence["result"] = ("DRY RUN — all preconditions and the signed "
                              "offer verified; NO signature produced, NO "
                              "funds moved. Not a payment, not revenue.")
        _save_state(state_path, {"version": STATE_VERSION, "status": "dry_run",
                                 "resource": resource_url})
        _write_evidence(evidence_path, evidence)
        _log("  DRY RUN complete — refused to sign; evidence written.")
        return 0

    # ===================== EXECUTE (guarded) =============================
    key = load_private_key(args.key_file)
    if not key:
        raise Refuse("no private key available (CANARY_PRIVATE_KEY env or "
                     "--key-file); refusing to execute")

    from eth_account import Account
    from x402 import x402ClientSync
    from x402.mechanisms.evm.exact import ExactEvmScheme
    from x402.mechanisms.evm.signers import EthAccountSigner
    from x402.http.utils import (decode_payment_required_header,
                                  encode_payment_signature_header,
                                  decode_payment_response_header)
    from x402.extensions.payment_identifier import (
        append_payment_identifier_to_extensions)

    account = Account.from_key(key)
    payer = account.address

    # idempotent one-shot: reuse a persisted signed payload on restart so the
    # server's payment-identifier cache returns the SAME result with NO second
    # settlement. The signed EIP-3009 authorization is a one-time,
    # amount-capped payment authorization — not a key — but still lives only in
    # the local state file.
    state = _load_state(state_path)
    if state.get("status") == "completed":
        _log("  state shows this canary already completed — nothing to do.")
        return 0

    pr = decode_payment_required_header(r0.headers[x402.PAYMENT_REQUIRED_HEADER])
    replayed_signed_payload = (
        "signed_payload" in state and state.get("resource") == resource_url
    )
    if replayed_signed_payload:
        sig_header = state["signed_payload"]
        _log("  reusing the persisted signed payload (idempotent replay)")
    else:
        client = x402ClientSync()
        client.register("eip155:*", ExactEvmScheme(EthAccountSigner(account)))
        extensions: dict[str, Any] = {}
        # official payment-identifier: a stable idempotency key for THIS canary
        append_payment_identifier_to_extensions(extensions)
        payload = client.create_payment_payload(pr, extensions=extensions or None)
        sig_header = encode_payment_signature_header(payload)
        # PERSIST BEFORE SENDING — a crash after this line re-sends the same
        # bytes; a crash before it never signed anything.
        state.update({"status": "signed", "resource": resource_url,
                      "payer": payer, "signed_payload": sig_header,
                      "amount_atomic": amount_atomic})
        _save_state(state_path, state)
        _log("  signed payload persisted; sending the paid request…")

    # 5. send the paid request
    rp = http.get(resource_url,
                  headers={x402.PAYMENT_SIGNATURE_HEADER: sig_header},
                  timeout=60)
    if rp.status_code != 200:
        state.update({"status": "unsettled", "last_status": rp.status_code})
        _save_state(state_path, state)
        raise Refuse(f"paid request returned {rp.status_code}: {rp.text[:300]}")
    result = rp.json()

    # 6. verify PAYMENT-RESPONSE + signed receipt + signed offer
    settle = decode_payment_response_header(rp.headers[x402.PAYMENT_RESPONSE_HEADER])
    tx = settle.transaction
    if not (settle.success and tx.startswith("0x") and len(tx) == 66):
        raise Refuse(f"PAYMENT-RESPONSE not a valid success: {settle}")
    if settle.network != MAINNET:
        raise Refuse(f"settlement network {settle.network} != {MAINNET}")
    receipt_ok = _verify_settle_receipt(rp, resource_url, payer, tx, http=http)
    if not receipt_ok:
        raise Refuse("signed receipt in PAYMENT-RESPONSE did not verify")

    # 7. INDEPENDENT on-chain confirmation (receipt + exact USDC transfer)
    conf = x402_confirm.confirm_settlement(
        tx, asset=facts["asset"], recipient=facts["recipient"],
        amount_atomic=str(amount_atomic))
    if not conf.get("confirmed"):
        raise Refuse(f"independent on-chain confirmation failed: "
                     f"{conf.get('reason')}")

    # 8. revenue rose by exactly one and contains this tx
    revenue_after = http.get(base + "/billing/revenue", timeout=20).json()
    rs_after = revenue_after.get("real_settlement", {})
    _verify_revenue_transition(
        rs_before, rs_after, tx,
        replayed_signed_payload=replayed_signed_payload,
    )

    state.update({"status": "completed", "transaction": tx})
    _save_state(state_path, state)

    evidence.update({
        "result": "settled_and_confirmed",
        "payer": payer,
        "transaction": tx,
        "block_number": conf.get("block_number"),
        "payment_response": {"success": settle.success, "network": settle.network,
                             "transaction": tx, "payer": settle.payer},
        "independent_confirmation": {k: conf.get(k) for k in
                                     ("confirmed", "reason", "block_number")},
        "revenue_after": rs_after,
        "idempotent_replay": replayed_signed_payload,
        "result_sha256": artifacts.sha256_hex(rp.content),
    })
    _write_evidence(evidence_path, evidence)
    _log(f"  SETTLED + CONFIRMED — tx {tx}; evidence written to {evidence_path}")
    _log("  NOTE: labelled first_party_mainnet_canary — NOT external adoption, "
         "NOT customer revenue.")
    return 0


def _verify_settle_receipt(response, resource_url: str, payer: str,
                           tx: str, http: Any = None) -> bool:
    """Verify the JWS-signed receipt carried in the PAYMENT-RESPONSE header's
    SettleResponse.extensions against the Guild signing key.  Resolve did:web
    from the trusted service origin exactly as the signed-offer verifier does;
    legacy self-describing did:key kids remain supported."""
    from app import x402
    from app import x402_artifacts as artifacts
    raw = json.loads(_b64(response.headers[x402.PAYMENT_RESPONSE_HEADER]))
    exts = raw.get("extensions") or {}
    receipt = ((exts.get("offer-receipt") or {}).get("info") or {}).get("receipt")
    if not receipt:
        return False
    jws = receipt.get("signature")
    kid = artifacts.jws_header(jws).get("kid", "")
    pub = _resolve_kid_public_key(kid, http)
    if not pub:
        return False
    payload = artifacts.jws_verify(jws, pub)
    return bool(payload and payload.get("resourceUrl") == resource_url
                and payload.get("transaction") == tx
                and (payload.get("payer") or "").lower() == payer.lower())


def _b64(s: str) -> bytes:
    import base64
    return base64.b64decode(s)


def _write_evidence(path: pathlib.Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2))


# Settlement-time SHA derivations for KNOWN historical settlements whose
# evidence was captured with the broken `sha` reader (empty production_sha).
# Keyed by transaction hash so the derivation can never attach to any other
# artifact. These are derived-from-deploy-history facts, labelled as such —
# never presented as a value captured at settlement time.
KNOWN_SETTLEMENT_SHA_DERIVATIONS: dict[str, dict[str, str]] = {
    "0x1052fa51aa1412119581194acc1011c51786a59538f46bb5f9d593f1ad16d802": {
        "sha": "7b095482f8cd8d88378737067530bc52dca040d1",
        "derived_from": (
            "deploy history, not capture: 7b09548 was the last commit pushed "
            "to main before the settlement (deployed 2026-07-17; verified live "
            "then), the settlement confirmed in Base block 0x2ea6123 at "
            "2026-07-21T07:26:33Z, and the NEXT deploy (b606ae5) was only "
            "committed at 2026-07-21T08:24:47Z — after the settlement. No "
            "other commit exists between them on main."),
        "confidence": "derived_from_deploy_history_not_captured",
    },
}


def repair_evidence(args) -> int:
    """Idempotently repair an evidence artifact whose production_sha capture
    failed (the pre-2026-07-22 reader read `sha`; /release serves `git_sha`).

    READ-ONLY against the target: performs GET /release only — no signing, no
    payment, no 402 flow. The captured `production_sha` field is left exactly
    as recorded (an empty capture stays an empty capture — history is not
    rewritten); the repair adds a clearly-labelled `production_sha_repair`
    block instead. Running it again is a no-op, so the artifact bytes are
    stable once repaired."""
    path = pathlib.Path(args.evidence)
    if not path.exists():
        _log(f"repair-evidence: no artifact at {path}")
        return 1
    evidence = json.loads(path.read_text())
    if evidence.get("production_sha"):
        _log("repair-evidence: production_sha already captured non-empty — "
             "nothing to repair")
        return 0
    if "production_sha_repair" in evidence:
        _log("repair-evidence: already repaired — idempotent no-op")
        return 0
    import httpx
    http = httpx.Client(follow_redirects=True)
    base = (evidence.get("target") or args.base).rstrip("/")
    live_sha = production_sha(http, base)
    tx = (evidence.get("transaction") or "").lower()
    repair: dict[str, Any] = {
        "reason": ("the canary's production_sha reader read `sha` from "
                   "GET /release, which serves `git_sha` — so the capture "
                   "at settlement time was silently empty (fixed 2026-07-22, "
                   "regression-pinned against the real /release schema)"),
        "read_only": True,
        "payment_made": False,
        "captured_production_sha_left_as_recorded": True,
        "live_git_sha_at_repair": live_sha,
        "repaired_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    deriv = KNOWN_SETTLEMENT_SHA_DERIVATIONS.get(tx)
    if deriv:
        repair["settlement_time_sha"] = dict(deriv)
    else:
        repair["settlement_time_sha"] = {
            "sha": None,
            "note": "no recorded derivation for this transaction — unknown, "
                    "honestly left absent"}
    evidence["production_sha_repair"] = repair
    _write_evidence(path, evidence)
    _log(f"repair-evidence: wrote {path.name} — live git_sha at repair "
         f"{live_sha or '(unverifiable)'}; settlement-time sha "
         f"{repair['settlement_time_sha'].get('sha') or 'unknown'} "
         "(derived, labelled as such). No payment was made.")
    return 0


def preflight(args) -> int:
    """SECRET-SILENT readiness preflight: validates the key file (existence,
    private permissions, accepted format), derives ONLY the public buyer
    address, and verifies live payment readiness — WITHOUT producing a
    signature or moving any funds. Prints only non-secret facts."""
    import httpx
    _log("Agent Guild mainnet canary — SECRET-SILENT PREFLIGHT "
         "(no signing, no payment)")
    ok = True

    facts = key_file_facts(args.key_file)
    _log(f"  key file: {facts['path']}")
    _log(f"    exists: {facts.get('exists')}")
    if facts.get("exists"):
        _log(f"    permissions: {facts.get('permissions_octal')} "
             f"(private: {facts.get('permissions_private')})")
        _log(f"    format: {facts.get('format')} "
             f"(accepted: {facts.get('format_accepted')})")
    ok &= bool(facts.get("exists") and facts.get("permissions_private")
               and facts.get("format_accepted"))

    try:
        addr = derive_public_address(args.key_file)
    except Refuse as e:
        addr = None
        _log(f"    address derivation refused: {e}")
    if addr:
        _log(f"  buyer public address: {addr}")
    else:
        _log("  buyer public address: (not derived — eth_account/x402[evm] "
             "not installed here; the address is public, deriving it needs "
             "the signing lib but produces no signature)")

    http = httpx.Client(follow_redirects=True, headers=_first_party_headers())
    base = args.base.rstrip("/")
    try:
        target_facts = verify_preconditions(http, base, args.expect_sha)
        _log(f"  readiness OK — network={target_facts['network']} "
             f"recipient={target_facts['recipient']} "
             f"facilitator={target_facts['facilitator_host']} "
             f"sha={target_facts['production_sha']}")
    except Refuse as e:
        _log(f"  readiness REFUSED: {e}")
        ok = False
    except Exception as e:  # noqa: BLE001
        _log(f"  readiness check failed: {type(e).__name__}: {e}")
        ok = False
    _log("PREFLIGHT " + ("PASSED — ready for --execute (after funding); "
                         "no signature was produced" if ok else "FAILED"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.environ.get("GUILD_BASE", DEFAULT_BASE))
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="(default) verify everything, sign NOTHING")
    mode.add_argument("--preflight", action="store_true",
                      help="secret-silent: validate the key file + derive "
                           "only the public address + check readiness; "
                           "never signs, never pays")
    mode.add_argument("--execute", action="store_true",
                      help="actually pay ONE trust decision (<= 0.01 USDC)")
    mode.add_argument("--repair-evidence", action="store_true",
                      help="idempotently repair a recorded evidence artifact "
                           "whose production_sha capture failed (read-only: "
                           "GET /release only — never signs, never pays)")
    ap.add_argument("--watch", action="store_true",
                    help="with --execute: wait for the buyer wallet to be "
                         "funded, then complete exactly once")
    ap.add_argument("--watch-interval", type=int, default=60)
    ap.add_argument("--watch-timeout", type=int, default=86400)
    ap.add_argument("--key-file", default=str(DEFAULT_KEY_FILE),
                    help="local key file (hex or JSON); or CANARY_PRIVATE_KEY "
                         f"env (default: {DEFAULT_KEY_FILE})")
    ap.add_argument("--expect-sha", default=None,
                    help="refuse to pay unless production /release SHA matches")
    ap.add_argument("--allow-untagged", action="store_true",
                    help="permit --execute WITHOUT GUILD_FIRST_PARTY_TOKEN "
                         "(the settlement will classify as unverified_payer "
                         "— normally refused)")
    ap.add_argument("--state",
                    default=str(REPO / "live" / "secrets" / "canary_state.json"),
                    help="one-shot state file (gitignored)")
    ap.add_argument("--evidence",
                    default=str(REPO / "artifacts"
                                / "first_party_mainnet_canary_evidence.json"))
    args = ap.parse_args()
    if args.execute:
        args.dry_run = False

    try:
        if args.preflight:
            return preflight(args)
        if args.repair_evidence:
            return repair_evidence(args)
        if args.execute and args.watch:
            return _watch_loop(args)
        return run(args)
    except Refuse as e:
        _log(f"REFUSED: {e}")
        return 1


def _watch_loop(args) -> int:
    """Wait for the buyer wallet to be funded (readable USDC balance ≥ the
    quoted amount), then complete exactly once. No further human input."""
    import httpx
    from app import x402, x402_confirm

    http = httpx.Client(follow_redirects=True)
    key = load_private_key(args.key_file)
    if not key:
        raise Refuse("watch mode needs a key (CANARY_PRIVATE_KEY / --key-file)")
    from eth_account import Account
    payer = Account.from_key(key).address
    usdc = x402.USDC_BY_NETWORK[MAINNET]
    rpc = x402_confirm.rpc_url()
    deadline = time.time() + args.watch_timeout
    _log(f"  watch: waiting for USDC funding of {payer} on Base…")
    while time.time() < deadline:
        data = "0x70a08231" + payer[2:].lower().rjust(64, "0")
        try:
            bal = http.post(rpc, json={"jsonrpc": "2.0", "id": 1,
                                       "method": "eth_call",
                                       "params": [{"to": usdc, "data": data},
                                                  "latest"]},
                            timeout=20).json().get("result")
            atomic = int(bal, 16) if bal else 0
        except Exception as e:  # noqa: BLE001
            _log(f"  watch: RPC read failed ({type(e).__name__}); retrying")
            atomic = 0
        if atomic >= LIFETIME_CAP_USDC * 1e6:
            _log(f"  watch: funded ({atomic/1e6:.6f} USDC) — executing once")
            return run(args)
        time.sleep(args.watch_interval)
    raise Refuse("watch timed out before the wallet was funded")


if __name__ == "__main__":
    sys.exit(main())
