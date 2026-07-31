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

import json
import re
import socket
import ssl
from typing import Any, Optional
from urllib.parse import urlsplit

from . import reachability

#: Checks that, when they FAIL, are strong evidence against delegating.
BLOCKING = ("endpoint_reachable", "protocol_handshake")

#: Card fields that assert the endpoint takes money.
_PAID_MARKERS = ("x402", "payment", "paid", "price", "usdc", "402")


def _probe_get(url: str, path: str, timeout_ctx=None
               ) -> tuple[Optional[int], bytes, str]:
    """SSRF-safe GET reusing the reachability module's pinned-address path.

    Same protections as ``liveness_probe``: URL policy check, DNS resolution
    with private/link-local/loopback screening, and a connection pinned to the
    screened address so a rebind between resolve and connect cannot redirect
    us. Never raises."""
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


def _card_is_signed(card: dict[str, Any]) -> tuple[bool, str]:
    """Is the Agent Card cryptographically signed?

    A2A cards carry signatures under `signatures` (JWS) in the spec drafts;
    Guild/DID-style cards may carry `proof`. We accept either, and we DO NOT
    verify the signature here — we report only that one is present, which is
    an honest, checkable statement. Claiming verification we did not perform
    would be exactly the failure this endpoint exists to expose."""
    if isinstance(card.get("signatures"), list) and card["signatures"]:
        return True, "JWS `signatures` present (presence only — not verified here)"
    if isinstance(card.get("proof"), dict) and card["proof"]:
        return True, "`proof` present (presence only — not verified here)"
    return False, "no `signatures` or `proof` on the card"


def _claims_payment(card: dict[str, Any]) -> bool:
    blob = json.dumps(card).lower() if card else ""
    return any(m in blob for m in _PAID_MARKERS)


def run(url: str, *, store=None) -> dict[str, Any]:
    """Run the preflight for one endpoint URL. Never raises."""
    checks: list[dict[str, Any]] = []

    # --- 1. reachability + REAL protocol handshake ------------------------
    rec = reachability.liveness_probe(url)
    status = rec.get("status")
    evidence = rec.get("evidence_level")
    if status == "recently_reachable" and evidence == "protocol_handshake":
        checks.append(_check("endpoint_reachable", "proven",
                             rec.get("detail") or "responded"))
        checks.append(_check(
            "protocol_handshake", "proven",
            "completed a real A2A/MCP handshake, not merely an HTTP 200"))
    elif status == "http_responsive":
        checks.append(_check("endpoint_reachable", "proven",
                             "something answered over HTTP"))
        checks.append(_check(
            "protocol_handshake", "failed",
            "a server answered but proved NO agent protocol. This is the "
            "single most common failure mode measured in the wild: 92.9% of "
            "listed agents report healthy, 33.9% actually complete a task."))
    else:
        checks.append(_check("endpoint_reachable", "failed",
                             rec.get("detail") or "no response"))
        checks.append(_check("protocol_handshake", "unknown",
                             "not attempted — the endpoint did not answer"))

    # --- 2. agent card: resolvable, and SIGNED? ---------------------------
    card: dict[str, Any] = {}
    truncated = False
    code, body, err = _probe_get(url, "/.well-known/agent-card.json")
    if code and 200 <= code < 300 and body:
        try:
            card = json.loads(body.decode("utf-8", "replace"))
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
        signed, why = _card_is_signed(card)
        checks.append(_check("agent_card_signed",
                             "proven" if signed else "failed", why))
    else:
        checks.append(_check("agent_card_resolves", "failed",
                             err or f"no parsable card (http {code})"))
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
                "payment_claim_holds", "failed",
                f"card advertises payment but the endpoint answered {pcode}, "
                "not 402. Measured in the wild: only 5.7% of self-declared "
                "paid agents actually challenge."))
    else:
        checks.append(_check("payment_claim_holds", "unknown",
                             "the card makes no payment claim to test"))

    # --- 4. independent evidence the Guild already holds ------------------
    known = None
    if store is not None and card:
        did = str(card.get("did") or (card.get("provider") or {}).get("did") or "")
        try:
            known = store.agent_by_did(did) if did else None
        except Exception:  # noqa: BLE001
            known = None
    if known:
        checks.append(_check(
            "independent_evidence", "proven",
            "this endpoint's DID is a registered Guild agent with an "
            "evidence history you can audit",
            agent_id=known.get("id"),
            attestations_received=known.get("attestations_received", 0)))
    else:
        checks.append(_check(
            "independent_evidence", "unknown",
            "no independent evidence — the Guild holds no attestation history "
            "for this endpoint. Absence of evidence is NOT evidence of risk; "
            "it means you are relying entirely on self-declaration."))

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
            "It works, but at least one of its own claims does not hold.")
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
            "We report presence of a card signature, not its validity. We do "
            "not execute a paid task. We cannot see what the endpoint does "
            "with your data. `unknowns` are excluded from the verdict rather "
            "than averaged into it, so a clean result over four unknowns is "
            "not the same as a clean result over eight checks — the counts "
            "are given so you can tell the difference."),
    }
