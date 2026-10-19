"""Delegation preflight — the check nobody performs, immediately before delegating.

WHY THIS AND NOT MORE PASSPORT SURFACE
--------------------------------------
Measured on 2026-07-31 across the live agent ecosystem:

  * a2aregistry lists 183 agents; 170 (92.9%) report `is_healthy: true`; only
    62 (33.9%) actually complete an A2A task when probed. **114 agents are
    green and broken at the same time.**
  * Of 3,913 agents with a valid Agent Card, 42 (0.8%) sign it. The A2A
    discovery specification itself states it prescribes no registry API, and
    contains no mention of signatures.
  * Of 2,459 agents self-labelled "paid", 141 (5.7%) actually answer with an
    HTTP 402 challenge. The other 94.3% is an unverified string in a card.
  * x402 `exact` is a PUSH payment: irreversible once executed, `payTo` is a
    bare address bound to no legal entity, and the documented remedy for a
    bad outcome is "the seller sends the money back". Escrow and reputation
    are both listed as future work.

So the risk sits in a specific place: the moment a caller is about to hand
work — and money — to an endpoint whose only assurances are self-declared.
Uptime monitoring answers the wrong question ("did something respond?"), and
every existing scorer grades a REPOSITORY or a static card, once, at
publication time.

WHAT THIS RETURNS
  A single unauthenticated call that separates CLAIMED from PROVEN for one
  endpoint, and says plainly which checks it could not perform. It never
  invents a score out of unknowns: an unknown is reported as unknown and is
  excluded from the verdict rather than being averaged into it.

WHAT IT IS NOT
  It is not a safety guarantee, not an endorsement, and not a substitute for
  the caller's own policy. It reports evidence and names its own limits, which
  is the entire difference between this and a green tick that means nothing.
"""
from __future__ import annotations

import base64
import json
import re
import socket
import ssl
from typing import Any, Optional
from urllib.parse import urlsplit

from . import a2a_card_signing
from . import crypto
from . import reachability

#: Checks that, when they FAIL, are strong evidence against delegating.
BLOCKING = ("endpoint_reachable", "protocol_handshake")

#: Card fields that assert the endpoint takes money.
_PAID_MARKERS = ("x402", "payment", "paid", "price", "usdc", "402")

#: Where a card signature's key may be resolved from. Every source is either
#: offline (did:key) or the TARGET'S OWN ORIGIN; a key published elsewhere
#: is never fetched, because a cross-origin key proves nothing about the
#: endpoint being checked.
_KEY_SOURCES = ("did:key", "did:web (same origin, /.well-known/did.json)",
                "jwks (same origin, /.well-known/jwks.json)")


def validate_target(url: Any) -> Optional[str]:
    """Why `url` cannot be preflighted at all, or None when it can be.

    Malformed input (an invalid port, a broken IPv6 literal, a bare host
    that is not an address) used to escape `urlsplit`/`.port` as ValueError
    and surface as a 500. A preflight must never raise, so the parse is
    done once, here, and the reason is returned as data."""
    if not isinstance(url, str) or not url.strip():
        return "empty target"
    if len(url) > 500:
        return "target longer than 500 characters"
    try:
        parts = urlsplit(url)
        _ = parts.port            # raises on a non-numeric / out-of-range port
        host = parts.hostname     # raises on a malformed IPv6 literal
    except ValueError as e:
        return f"malformed url: {e}"
    if parts.scheme not in ("http", "https"):
        return f"unsupported scheme {parts.scheme!r} (http/https only)"
    if not host:
        return "missing host"
    return None


def _origin(url: str) -> str:
    """scheme://host[:port], lower-cased — the identity-binding unit."""
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        port = p.port
    except ValueError:
        return ""
    if not host:
        return ""
    default = 443 if p.scheme == "https" else 80
    if port and port != default:
        return f"{p.scheme}://{host}:{port}"
    return f"{p.scheme}://{host}"


def _probe_get(url: str, path: str, timeout_ctx=None
               ) -> tuple[Optional[int], bytes, str]:
    """SSRF-safe GET reusing the reachability module's pinned-address path.

    Same protections as ``liveness_probe``: URL policy check, DNS resolution
    with private/link-local/loopback screening, and a connection pinned to the
    screened address so a rebind between resolve and connect cannot redirect
    us. Never raises."""
    bad = validate_target(url)
    if bad:
        return None, b"", bad
    ok, reason = reachability.url_policy_check(url)
    if not ok:
        return None, b"", f"policy: {reason}"
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        return None, b"", "no host"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    ok, addrs, reason = reachability._resolve_and_screen(host, port)
    if not ok:
        return None, b"", reason
    family, addr = addrs[0]
    try:
        code, body = reachability._http_request_pinned(
            parts.scheme, host, family, addr, port, path, "GET", None, "", None)
        return code, (body or b""), ""
    except (ssl.SSLError, socket.error, OSError) as e:
        return None, b"", f"{type(e).__name__}"
    except Exception as e:  # noqa: BLE001 — a preflight must never 500
        return None, b"", f"{type(e).__name__}"


def _check(name: str, status: str, detail: str, **extra) -> dict[str, Any]:
    """status is one of: proven | failed | unknown."""
    return {"check": name, "status": status, "detail": detail, **extra}


# ---------------------------------------------------------------------------
# agent card signature: presence, validity and identity binding — kept apart
# ---------------------------------------------------------------------------

def _b64url_json(s: Any) -> Optional[dict[str, Any]]:
    try:
        raw = base64.urlsafe_b64decode(str(s) + "=" * (-len(str(s)) % 4))
        obj = json.loads(raw.decode("utf-8"))
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        return None


def _hex_from_multibase(mb: Any) -> Optional[str]:
    """Ed25519 public key hex from a Multikey / did:key multibase value."""
    try:
        raw = crypto.multibase_b58btc_decode(str(mb))
    except (ValueError, TypeError):
        return None
    if len(raw) == 34 and raw[0] == 0xED and raw[1] == 0x01:
        return raw[2:].hex()
    if len(raw) == 32:
        return raw.hex()
    return None


def _hex_from_jwk(jwk: Any) -> Optional[str]:
    if not isinstance(jwk, dict):
        return None
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        return None
    try:
        x = str(jwk.get("x") or "")
        raw = base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))
    except (ValueError, TypeError):
        return None
    return raw.hex() if len(raw) == 32 else None


def _resolve_signing_key(url: str, card: dict[str, Any], kid: str
                         ) -> tuple[Optional[str], str, str]:
    """(public_hex, source, reason). The key for a card signature is accepted
    from exactly three places, in this order: a did:key (offline, the key IS
    the identifier); the target's OWN did:web document; the target's OWN
    JWKS. The card's `did` (did:key) is consulted when the header names no
    resolvable kid. Nothing cross-origin is ever fetched."""
    target_did_web = crypto.did_web_from_origin(_origin(url)) if _origin(url) else ""
    base_kid = kid.split("#", 1)[0]
    if kid.startswith("did:key:"):
        try:
            return crypto.public_key_from_did(base_kid), "did:key (kid)", ""
        except (ValueError, TypeError):
            return None, "", "kid is a did:key this verifier cannot decode"
    if kid.startswith("did:web:"):
        if base_kid != target_did_web:
            return None, "", (f"kid names {base_kid}, which is not this "
                              f"endpoint's origin ({target_did_web}); a "
                              "cross-origin key is not resolved")
        code, body, err = _probe_get(url, "/.well-known/did.json")
        if not (code and 200 <= code < 300 and body):
            return None, "", f"did:web document not readable ({err or code})"
        try:
            doc = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return None, "", "did:web document is not JSON"
        if not isinstance(doc, dict):
            return None, "", "did:web document is not an object"
        for vm in doc.get("verificationMethod") or []:
            if isinstance(vm, dict) and vm.get("id") == kid:
                hexkey = (_hex_from_multibase(vm.get("publicKeyMultibase"))
                          or _hex_from_jwk(vm.get("publicKeyJwk")))
                if hexkey:
                    return hexkey, "did:web (same origin)", ""
                return None, "", "verification method is not an Ed25519 key"
        return None, "", "kid not found in the endpoint's did:web document"
    card_did = str(card.get("did") or "")
    if card_did.startswith("did:key:"):
        try:
            return crypto.public_key_from_did(card_did), "did:key (card `did`)", ""
        except (ValueError, TypeError):
            pass
    if kid:
        code, body, err = _probe_get(url, "/.well-known/jwks.json")
        if code and 200 <= code < 300 and body:
            try:
                jwks = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                jwks = None
            if isinstance(jwks, dict):
                for jwk in jwks.get("keys") or []:
                    if isinstance(jwk, dict) and jwk.get("kid") == kid:
                        hexkey = _hex_from_jwk(jwk)
                        if hexkey:
                            return hexkey, "jwks (same origin)", ""
                        return None, "", "JWKS key is not an Ed25519 OKP key"
                return None, "", "kid not found in the endpoint's JWKS"
        return None, "", (f"no key source for kid {kid[:80]!r} on this "
                          "origin (not did:key, not this origin's did:web, "
                          "no same-origin JWKS entry)")
    return None, "", ("the signature names no key (no kid) and the card "
                      "carries no did:key")


def _card_signature(url: str, card: dict[str, Any]) -> dict[str, Any]:
    """The `agent_card_signed` check with its claim made precise.

    Three different statements are kept apart and reported as data:
      presence      — a `signatures` (JWS) or `proof` member exists;
      validity      — the JWS verified under a key resolved from an
                      ACCEPTABLE source (see _KEY_SOURCES);
      binding       — the verified key is also an identity (did:key) that can
                      be compared with a registered Guild agent.
    status: proven = verified; failed = absent OR present-but-invalid under
    the key it names; unknown = present but validity not established."""
    sigs = card.get("signatures")
    if isinstance(sigs, list) and sigs:
        first_reason = ""
        for entry in sigs:
            if not isinstance(entry, dict):
                continue
            header = _b64url_json(entry.get("protected")) or {}
            alg = str(header.get("alg") or "")
            kid = str(header.get("kid") or "")
            if alg != "EdDSA":
                first_reason = first_reason or (
                    f"alg {alg or 'missing'!r} is not supported by this "
                    "verifier (EdDSA only)")
                continue
            hexkey, source, reason = _resolve_signing_key(url, card, kid)
            if not hexkey:
                first_reason = first_reason or reason
                continue
            single = dict(card)
            single["signatures"] = [entry]
            ok = a2a_card_signing.verify_agent_card(single, hexkey)
            if ok:
                return _check(
                    "agent_card_signed", "proven",
                    f"JWS EdDSA signature VERIFIED under a key resolved from "
                    f"{source}; this proves the card's integrity and that the "
                    "holder of that key signed it — not safety, not "
                    "competence",
                    verification="verified", key_source=source,
                    kid=kid[:160] or None,
                    signer_did_key=crypto.did_from_public_key(hexkey))
            return _check(
                "agent_card_signed", "failed",
                f"a JWS signature is present but did NOT verify under the "
                f"key it names (resolved from {source}); treat the card as "
                "tampered or mis-signed",
                verification="invalid", key_source=source,
                kid=kid[:160] or None)
        return _check(
            "agent_card_signed", "unknown",
            "a JWS signature is present but its validity could not be "
            f"established: {first_reason or 'no verifiable entry'}. Presence "
            "is not verification.",
            verification="unverified_presence")
    if isinstance(card.get("proof"), dict) and card["proof"]:
        return _check(
            "agent_card_signed", "unknown",
            "a Data Integrity `proof` is present; this check does not verify "
            "that proof type, so validity is unknown. Presence is not "
            "verification.",
            verification="unverified_presence")
    return _check("agent_card_signed", "failed",
                  "no `signatures` or `proof` on the card",
                  verification="absent")


def _claims_payment(card: dict[str, Any]) -> bool:
    blob = json.dumps(card).lower() if card else ""
    return any(m in blob for m in _PAID_MARKERS)


_NO_EVIDENCE = ("no independent evidence — the Guild holds no attestation "
                "history for this endpoint. Absence of evidence is NOT "
                "evidence of risk; it means you are relying entirely on "
                "self-declaration.")


def _independent_evidence(url: str, card: dict[str, Any],
                          checks: list[dict[str, Any]], store) -> dict[str, Any]:
    """The `independent_evidence` check.

    proven  — a registered Guild agent is BOUND to this endpoint (see
              `binding`) AND the Guild holds ≥1 attestation for it.
    unknown — everything else, with the reason stated: no claimed identity;
              a claimed identity nothing binds to this endpoint (reported as
              `identity_match: claimed_unverified`); or a bound identity with
              no attestation history (nothing to audit)."""
    claimed = ""
    if card:
        provider = card.get("provider") if isinstance(card.get("provider"), dict) else {}
        claimed = str(card.get("did") or provider.get("did") or "")
    if not claimed or store is None:
        return _check("independent_evidence", "unknown", _NO_EVIDENCE)
    try:
        known = store.agent_by_did(claimed)
    except Exception:  # noqa: BLE001
        known = None
    if not known:
        return _check("independent_evidence", "unknown", _NO_EVIDENCE,
                      claimed_did=claimed[:160],
                      identity_match="no_registered_agent")
    sig = next((c for c in checks if c["check"] == "agent_card_signed"), {})
    binding = None
    if (sig.get("verification") == "verified"
            and sig.get("signer_did_key") == str(known.get("did") or "")):
        binding = "card_signature"
    else:
        declared = str((known.get("metadata") or {}).get("endpoint") or "")
        if declared and _origin(declared) and _origin(declared) == _origin(url):
            binding = "registered_endpoint_declaration"
    try:
        attestations = len(store.attestations_for(known.get("id")))
    except Exception:  # noqa: BLE001
        attestations = 0
    if binding is None:
        return _check(
            "independent_evidence", "unknown",
            "the card names a registered Guild DID, but nothing binds THIS "
            "endpoint to that identity: the card is not verifiably signed by "
            "that agent's key and the agent's registration declares no "
            "endpoint at this origin. This is a claimed identity match, "
            "unverified — do not read it as evidence.",
            claimed_did=claimed[:160], identity_match="claimed_unverified",
            attestations_received=attestations)
    if attestations == 0:
        return _check(
            "independent_evidence", "unknown",
            f"identity bound to this endpoint ({binding}), but the Guild holds "
            "no attestation history for it — there is nothing to audit. "
            "Absence of evidence is NOT evidence of risk.",
            agent_id=known.get("id"), binding=binding,
            identity_match="bound", attestations_received=0)
    return _check(
        "independent_evidence", "proven",
        f"a registered Guild agent is bound to this endpoint ({binding}) and "
        f"has {attestations} attestation(s) you can audit",
        agent_id=known.get("id"), binding=binding, identity_match="bound",
        attestations_received=attestations)


def _invalid_target(url: Any, reason: str) -> dict[str, Any]:
    """The result for a target that cannot be addressed at all. Every check
    is `unknown` except reachability, which is `failed` for the stated
    reason; nothing about the endpoint is inferred from a bad URL."""
    checks = [
        _check("endpoint_reachable", "failed", f"invalid target: {reason}"),
        _check("protocol_handshake", "unknown", "not attempted — invalid target"),
        _check("agent_card_resolves", "unknown", "not attempted — invalid target"),
        _check("agent_card_signed", "unknown", "not attempted — invalid target"),
        _check("payment_claim_holds", "unknown", "not attempted — invalid target"),
        _check("independent_evidence", "unknown", "not attempted — invalid target"),
    ]
    return {
        "target": str(url)[:500] if isinstance(url, str) else "",
        "input_error": reason,
        "verdict": "do_not_delegate",
        "headline": "The target could not be parsed as an http(s) URL; nothing "
                    "was probed.",
        "checks": checks,
        "failed": ["endpoint_reachable"],
        "unknowns": [c["check"] for c in checks if c["status"] == "unknown"],
        "scored": ["endpoint_reachable"],
        "method": "input validation only — no network request was made",
        "limits": "a malformed target says nothing about any endpoint",
    }


def run(url: str, *, store=None) -> dict[str, Any]:
    """Run the preflight for one endpoint URL. Never raises."""
    bad = validate_target(url)
    if bad:
        return _invalid_target(url, bad)
    try:
        return _run(url, store=store)
    except Exception as e:  # noqa: BLE001 — a preflight must never 500
        return _invalid_target(url, f"internal error during probe: "
                                    f"{type(e).__name__}")


def _run(url: str, *, store=None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    # --- 1. reachability + REAL protocol handshake ------------------------
    rec = reachability.liveness_probe(url)
    status = rec.get("status")
    evidence = rec.get("evidence_level")
    protocol_probe = rec.get("protocol_probe") or {}
    mcp_proven = (status == "recently_reachable"
                  and evidence == "protocol_handshake"
                  and protocol_probe.get("protocol") == "mcp"
                  and protocol_probe.get("result") == "proven")
    http_only = (status == "http_responsive"
                 and evidence == "http_response"
                 and protocol_probe.get("protocol") == "unspecified"
                 and protocol_probe.get("result") == "not_attempted")
    if status == "recently_reachable" and evidence == "protocol_handshake":
        checks.append(_check("endpoint_reachable", "proven",
                             rec.get("detail") or "responded"))
        checks.append(_check(
            "protocol_handshake", "proven",
            "received an A2A card or valid MCP initialization response; "
            "this does not prove successful task execution"))
    elif status == "http_responsive":
        checks.append(_check("endpoint_reachable", "proven",
                             "something answered over HTTP"))
        probe_result = (rec.get("protocol_probe") or {}).get("result")
        incomplete = probe_result in (
            "authorization_required", "inconclusive", "not_attempted")
        checks.append(_check(
            "protocol_handshake", "unknown" if incomplete else "failed",
            "no protocol probe applies to this endpoint path" if probe_result == "not_attempted"
            else rec.get("detail") if incomplete else
            "the unauthenticated bounded probe did not observe a successful "
            "agent protocol handshake; an HTTP response alone is not protocol proof"))
    else:
        checks.append(_check("endpoint_reachable", "failed",
                             rec.get("detail") or "no response"))
        checks.append(_check("protocol_handshake", "unknown",
                             "not attempted — the endpoint did not answer"))

    # --- 2. agent card: resolvable, and SIGNED? ---------------------------
    card: dict[str, Any] = {}
    truncated = False
    non_object = False
    code, body, err = _probe_get(url, "/.well-known/agent-card.json")
    if code and 200 <= code < 300 and body:
        try:
            parsed = json.loads(body.decode("utf-8", "replace"))
            if isinstance(parsed, dict):
                card = parsed
            else:
                # Valid JSON that is not an object (a list, a string, a
                # number, null) is not an Agent Card. Previously this reached
                # `card.get(...)` and raised — a 500 from the one endpoint
                # that must never raise.
                non_object = True
        except (ValueError, UnicodeDecodeError):
            # The probe caps its read, so a LARGE but perfectly valid card
            # arrives truncated and will not parse. Treating that as "no card"
            # would report a false failure against a well-formed agent — the
            # precise error class this endpoint exists to eliminate. A
            # truncated card is reported as RESOLVING, and every check that
            # needs the whole document degrades to `unknown`, never to
            # `failed`.
            text = body.decode("utf-8", "replace").lstrip()
            if text.startswith("{") and not text.rstrip().endswith("}"):
                truncated = True
    if truncated:
        checks.append(_check(
            "agent_card_resolves", "proven",
            f"served at /.well-known/agent-card.json ({code}), but larger "
            "than the probe read cap — inspected only in part"))
        checks.append(_check(
            "agent_card_signed", "unknown",
            "the card exceeded the bounded probe read, so the absence of a "
            "signature cannot be asserted (reported as unknown, NOT as a "
            "failure)"))
        checks.append(_check(
            "payment_claim_holds", "unknown",
            "not tested — the card could not be read in full"))
    elif card:
        checks.append(_check("agent_card_resolves", "proven",
                             f"served at /.well-known/agent-card.json ({code})",
                             declared_name=str(card.get("name") or "")[:120]))
        checks.append(_card_signature(url, card))
    else:
        # Neither observed MCP nor an HTTP-only probe requires an A2A card.
        # Only a 404 is optional absence; existing card defects stay visible.
        optional_absent_card = code == 404 and (mcp_proven or http_only)
        card_detail = err or f"no parsable card (http {code})"
        if non_object:
            card_detail = (f"served at /.well-known/agent-card.json ({code}) "
                           "but the body is JSON that is not an object — "
                           "not an Agent Card")
        elif optional_absent_card:
            card_detail = (
                "no A2A card (http 404); not required for the observed MCP handshake"
                if mcp_proven else
                "no A2A card (http 404); no A2A protocol probe applies to this HTTP endpoint")
        checks.append(_check("agent_card_resolves",
                             "unknown" if optional_absent_card else "failed",
                             card_detail))
        checks.append(_check("agent_card_signed", "unknown",
                             "not attempted — no card to inspect"))

    # --- 3. does a payment CLAIM actually hold? ---------------------------
    if truncated:
        pass                      # already reported as unknown above
    elif card and _claims_payment(card):
        pcode, _pbody, perr = _probe_get(url, "/")
        if pcode == 402:
            checks.append(_check("payment_claim_holds", "proven",
                                 "returned a 402 payment challenge as claimed"))
        elif pcode is None:
            checks.append(_check("payment_claim_holds", "unknown",
                                 perr or "could not probe the paid surface"))
        else:
            checks.append(_check(
                "payment_claim_holds", "unknown",
                f"the discovery root answered {pcode}, not 402. A free root "
                "can coexist with paid operations; the card's paid operation "
                "was not executed, so its payment claim is unverified."))
    else:
        checks.append(_check("payment_claim_holds", "unknown",
                             "the card makes no payment claim to test"))

    # --- 4. independent evidence the Guild already holds ------------------
    # A card can name ANY DID. Until 2026-10-14 a registered DID string in
    # a card was reported as `proven` independent evidence — with zero
    # attestations, and with nothing binding this endpoint to that identity.
    # Now: the claimed identity must be BOUND to this endpoint (the card's
    # verified signature is that agent's key, or the registered agent itself
    # declared an endpoint at this origin), AND the Guild must actually hold
    # attestations for it. Anything less is a claimed, unverified match.
    checks.append(_independent_evidence(url, card, checks, store))

    # --- verdict ----------------------------------------------------------
    by_name = {c["check"]: c for c in checks}
    failed_blocking = [n for n in BLOCKING if by_name[n]["status"] == "failed"]
    failed_other = [c["check"] for c in checks
                    if c["status"] == "failed" and c["check"] not in BLOCKING]
    unknown = [c["check"] for c in checks if c["status"] == "unknown"]

    if failed_blocking:
        verdict, headline = "do_not_delegate", (
            "This endpoint did not prove it can do the thing it is listed for.")
    elif failed_other:
        verdict, headline = "delegate_with_caution", (
            "At least one check failed; inspect its details and the unknowns "
            "before deciding whether to delegate.")
    else:
        verdict, headline = "no_failed_checks", (
            "Every check we could perform passed. That is not an endorsement — "
            "see `unknowns`.")

    return {
        "target": url,
        "verdict": verdict,
        "headline": headline,
        "checks": checks,
        "failed": failed_blocking + failed_other,
        "unknowns": unknown,
        "scored": [c["check"] for c in checks if c["status"] != "unknown"],
        "method": ("live probe at request time — NOT a cached badge and NOT a "
                   "score computed from a repository at publication time. A "
                   "server can change its behaviour after any one-off review; "
                   "this is the state we observed just now."),
        "limits": (
            "A card signature is VERIFIED only when its key resolves from "
            "the card's did:key or this origin's own did:web/JWKS (EdDSA); "
            "otherwise its presence is reported as unknown validity, never "
            "as proof. A verified signature proves integrity and key "
            "control, not safety. `independent_evidence` is proven only when "
            "a registered identity is bound to this endpoint AND has an "
            "attestation history; a DID merely named in a card is a claim. "
            "We do not execute a paid task. We cannot see what the endpoint "
            "does with your data. `unknowns` are excluded from the verdict "
            "rather than averaged into it, so a clean result over four "
            "unknowns is not the same as a clean result over eight checks — "
            "the counts are given so you can tell the difference."),
    }
