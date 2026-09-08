"""Guild client with outage fallback — VERIFY BEFORE USE.

Fetches SIGNED decisions (GET /check?signed=true) and passports. Corrective
pass 2026-07-13: a live document is returned as channel="live" ONLY after it

  1. cryptographically verifies (eddsa-jcs-2022 against the issuer did:key),
  2. comes from an allowed/pinned issuer — a changed issuer is accepted only
     via a VERIFIED dual-signed rotation chain fetched from /ledger/rotations,
  3. is inside its validity window,
  4. is AGD-1 conformant (when a decision is present), and
  5. satisfies the one-counterparty binding (decision == routed provider).

A verification failure is an UNVERIFIED state (channel="unverified"), never
"live": the cache is consulted, and if nothing verifiable exists the gateway
fails according to policy (enforce mode denies). stdlib urllib only —
integrators can vendor this file.

Transport rules (distribution pass 2026-09-08):

* **No automatic payment, ever.** A priced route answers HTTP 402 with an
  x402 ``PAYMENT-REQUIRED`` quote. The client surfaces it as
  :class:`PaymentRequired` / channel ``"payment_required"`` and stops. It never
  signs, retries with credentials, or settles anything — paying is the
  caller's explicit act, outside this library.
* **Redirects are refused.** ``urllib`` would otherwise replay ``X-API-Key``
  to whatever host a 3xx names. A redirect is reported as
  :class:`GuildRedirect` and treated as no evidence.
* **Responses are bounded** (:data:`MAX_RESPONSE_BYTES`); an oversized body
  is :class:`ResponseTooLarge`, not a memory hazard.
* **Passports are bound and fresh.** A passport is returned only when it
  verifies, its issuer is allowed, it is inside ``validFrom``/``validUntil``
  and its subject matches what was asked for (see :meth:`GuildClient.passport_result`).
* **Preflight is free and unsigned.** :meth:`GuildClient.preflight` is a live
  observation of an endpoint, not verifiable evidence; ``unknowns`` are
  reported, never folded into the verdict.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from ._version import __version__
from .cache import SignedDecisionCache
from .contract import validate_decision, binding_violations
from .verify import verify_data_integrity, within_validity

DEFAULT_BASE = "https://agent-guild-5d5r.onrender.com"
UA = f"agentguild-trustplane/{__version__}"

#: Hard upper bound on any Guild response body read by this client.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

#: Verdicts GET /preflight is documented to return.
PREFLIGHT_VERDICTS = ("no_failed_checks", "delegate_with_caution",
                      "do_not_delegate")

PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"

#: Attribution tag sent with an explicit registration (RegisterRequest.src,
#: pattern ^[a-z0-9_:-]+$). Names the surface honestly; pass src=None to omit.
REGISTER_SRC = "pypi:agentguild-trustplane"
_SRC_RE = re.compile(r"^[a-z0-9_:-]{1,64}$")


class GuildError(Exception):
    """Base class for transport-level failures talking to the Guild."""


class GuildHTTPError(GuildError):
    """A non-2xx answer. ``body`` is the parsed JSON body when there was one."""

    def __init__(self, status: int, path: str, body: Any = None,
                 headers: Optional[dict[str, str]] = None) -> None:
        self.status = status
        self.path = path
        self.body = body
        self.headers = headers or {}
        super().__init__(f"HTTP {status} from {path}")


class PaymentRequired(GuildHTTPError):
    """The Guild quoted a price (HTTP 402) instead of serving the resource.

    ``quote`` is the JSON 402 body (x402 v2 ``accepts`` / ``resource`` plus
    the Guild's ``sandbox`` note); ``payment_required_header`` is the raw
    ``PAYMENT-REQUIRED`` header when present. Nothing has been paid and
    nothing will be paid by this library."""

    def __init__(self, path: str, quote: Any,
                 headers: Optional[dict[str, str]] = None) -> None:
        super().__init__(402, path, quote, headers)
        self.quote = quote if isinstance(quote, dict) else {}
        self.payment_required_header = (headers or {}).get(
            PAYMENT_REQUIRED_HEADER) or (headers or {}).get(
            PAYMENT_REQUIRED_HEADER.lower())

    @property
    def terms(self) -> list[dict[str, Any]]:
        """Payment options exactly as quoted (see :func:`quote_terms`)."""
        return quote_terms(self.quote, self.payment_required_header)

    def __str__(self) -> str:
        terms = self.terms
        price = ""
        if terms and terms[0].get("amount") is not None:
            t = terms[0]
            price = (f" — quoted {t['amount']} (atomic units) of {t['asset']} "
                     f"on {t['network']} ({t['scheme']})")
        return f"payment required for {self.path}{price}; not paid"


def decode_payment_required_header(value: Optional[str]) -> Optional[dict[str, Any]]:
    """Decode the x402 v2 ``PAYMENT-REQUIRED`` header (base64 JSON). Returns
    None when absent or undecodable. Pure; no network."""
    if not value:
        return None
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4))
        obj = json.loads(raw.decode("utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def quote_terms(quote: Optional[dict[str, Any]],
                payment_required_header: Optional[str] = None) -> list[dict[str, Any]]:
    """The payment options a 402 actually offered — read from the response,
    never from a price table. Each entry: scheme, network, asset, amount
    (atomic units, as quoted), payTo, maxTimeoutSeconds, resource. The
    header is authoritative under x402 v2; the JSON body mirrors it. Empty
    list when neither carries an ``accepts`` array."""
    src = None
    hdr = decode_payment_required_header(payment_required_header)
    if hdr and isinstance(hdr.get("accepts"), list):
        src = hdr
    elif isinstance(quote, dict) and isinstance(quote.get("accepts"), list):
        src = quote
    if not src:
        return []
    resource = src.get("resource")
    resource_url = resource.get("url") if isinstance(resource, dict) else resource
    out = []
    for a in src["accepts"]:
        if not isinstance(a, dict):
            continue
        out.append({
            "scheme": a.get("scheme"), "network": a.get("network"),
            "asset": a.get("asset"), "amount": a.get("amount"),
            "payTo": a.get("payTo"),
            "maxTimeoutSeconds": a.get("maxTimeoutSeconds"),
            "resource": resource_url,
        })
    return out


class GuildRedirect(GuildError):
    """The Guild answered with a redirect, which this client never follows."""

    def __init__(self, status: int, path: str, location: Optional[str]) -> None:
        self.status = status
        self.path = path
        self.location = location
        super().__init__(f"HTTP {status} redirect from {path} refused")


class ResponseTooLarge(GuildError):
    def __init__(self, path: str, limit: int) -> None:
        super().__init__(f"response from {path} exceeds {limit} bytes")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _read_bounded(resp: Any, path: str) -> bytes:
    data = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ResponseTooLarge(path, MAX_RESPONSE_BYTES)
    return data


def _parse_json(data: bytes) -> Any:
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


@dataclass
class PreflightResult:
    """The free ``GET /preflight`` answer for one endpoint, as observed live.

    Not signed and not cacheable evidence: it says what the endpoint proved
    at request time. ``unknowns`` are checks the Guild could not perform; they
    are excluded from ``verdict`` rather than averaged into it."""
    target: str
    verdict: str
    headline: str
    checks: list[dict[str, Any]]
    failed: list[str]
    unknowns: list[str]
    scored: list[str]
    method: str
    limits: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def known_verdict(self) -> bool:
        return self.verdict in PREFLIGHT_VERDICTS

    @property
    def no_failed_checks(self) -> bool:
        return self.verdict == "no_failed_checks"

    @property
    def do_not_delegate(self) -> bool:
        return self.verdict == "do_not_delegate"


@dataclass
class PassportResult:
    """Outcome of fetching + verifying an Agent Passport.

    ``channel``: ``live`` (fetched and fully verified now), ``cache`` (served
    from the signed cache; ``state`` says fresh/stale), ``unverified`` (the
    Guild answered but the credential failed a check — see ``reason``),
    ``payment_required``, ``not_found`` or ``outage``. ``doc`` is None unless
    the passport is acceptable."""
    agent_id: str
    doc: Optional[dict[str, Any]]
    channel: str
    reason: Optional[str] = None
    state: Optional[str] = None
    age_seconds: Optional[float] = None
    subject_did: Optional[str] = None
    issuer_did: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.doc is not None


def _passport_subject_ok(agent_id: str, doc: dict[str, Any]) -> Optional[str]:
    """Bind the credential to the identity that was asked for. Returns a
    failure reason, or None when the binding holds."""
    subject = doc.get("credentialSubject")
    if not isinstance(subject, dict) or not subject.get("id"):
        return "credentialSubject.id missing"
    types = doc.get("type")
    if isinstance(types, str):
        types = [types]
    if not isinstance(types, list) or "AgentGuildPassport" not in types:
        return "not an AgentGuildPassport credential"
    if agent_id.startswith("did:"):
        if subject["id"] != agent_id:
            return (f"subject mismatch: credential is about {subject['id']!r}, "
                    f"not {agent_id!r}")
        return None
    # A Guild-local id cannot be matched against the subject DID directly;
    # the issuer stamps it into the credential id (urn:passport:<id>:<ts>).
    cred_id = doc.get("id")
    if isinstance(cred_id, str) and cred_id.startswith("urn:passport:"):
        parts = cred_id.split(":")
        if len(parts) >= 4 and parts[2] != agent_id:
            return (f"subject mismatch: credential id names {parts[2]!r}, "
                    f"not {agent_id!r}")
    return None


class GuildClient:
    def __init__(self, base_url: str = DEFAULT_BASE,
                 cache: Optional[SignedDecisionCache] = None,
                 timeout: float = 15.0,
                 api_key: Optional[str] = None,
                 extra_headers: Optional[dict[str, str]] = None) -> None:
        self.base = base_url.rstrip("/")
        self.cache = cache
        self.timeout = timeout
        self.api_key = api_key
        # in-process pin fallback when no cache directory is configured
        self._local_pins: list[str] = []
        self.stats = {"live_fetches": 0, "cache_serves": 0, "outages": 0,
                      "live_verify_failures": 0, "payment_required": 0,
                      "redirects_refused": 0}
        self.last_verify_failure: Optional[str] = None
        #: the most recent 402 quote (a PaymentRequired), never acted upon
        self.last_payment_required: Optional[PaymentRequired] = None
        self.last_error: Optional[BaseException] = None
        # Extra headers on every request (e.g. an operator's first-party
        # marker). Never inspected or logged by the client.
        self.extra_headers: dict[str, str] = dict(extra_headers or {})

    # -- transport --------------------------------------------------------
    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        headers = {"User-Agent": UA}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        headers.update(self.extra_headers)
        headers.update(extra or {})
        return headers

    def _request(self, path: str, *, method: str = "GET",
                 body: Optional[dict[str, Any]] = None,
                 extra_headers: Optional[dict[str, str]] = None) -> Any:
        """One bounded, redirect-free request. Returns the parsed JSON body.

        Raises PaymentRequired (402), GuildRedirect (3xx), ResponseTooLarge,
        GuildHTTPError (other non-2xx) or urllib/socket errors (unreachable).
        The 402 is recorded on ``last_payment_required``; it is never paid."""
        headers = self._headers(extra_headers)
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=headers, method=method)
        try:
            with _OPENER.open(req, timeout=self.timeout) as r:
                raw = _read_bounded(r, path)
        except urllib.error.HTTPError as e:
            status = e.code
            hdrs = {k: v for k, v in e.headers.items()} if e.headers else {}
            if 300 <= status < 400:
                self.stats["redirects_refused"] += 1
                err: GuildError = GuildRedirect(status, path, hdrs.get("Location"))
                self.last_error = err
                raise err from None
            try:
                parsed = _parse_json(_read_bounded(e, path))
            except ResponseTooLarge:
                parsed = None
            finally:
                e.close()
            if status == 402:
                self.stats["payment_required"] += 1
                pr = PaymentRequired(path, parsed, hdrs)
                self.last_payment_required = pr
                self.last_error = pr
                raise pr from None
            err = GuildHTTPError(status, path, parsed, hdrs)
            self.last_error = err
            raise err from None
        except GuildError:
            raise
        except Exception as e:  # unreachable, timeout, TLS, DNS
            self.last_error = e
            raise
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            err = GuildHTTPError(200, path, None)
            err.args = (f"non-JSON body from {path}: {e}",)
            self.last_error = err
            raise err from None

    def _get(self, path: str) -> dict[str, Any]:
        return self._request(path)

    def _post(self, path: str, body: dict[str, Any],
              extra_headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
        return self._request(path, method="POST", body=body,
                             extra_headers=extra_headers)

    # -- issuer acceptance -----------------------------------------------------
    def _issuer_allowed(self, issuer_did: str) -> bool:
        """Pinned/TOFU acceptance, with a verified-rotation-chain path for a
        changed issuer. Never accepts an unproven issuer change."""
        if self.cache is not None:
            if self.cache.issuer_ok(issuer_did):
                return True
            # unknown issuer: try to prove continuity via the rotation chain
            try:
                rot = self._get("/ledger/rotations").get("rotations") or []
            except Exception:
                return False
            return self.cache.accept_rotation(issuer_did, rot)
        # no cache: in-process TOFU (still never silently re-pins)
        if not self._local_pins:
            self._local_pins.append(issuer_did)
            return True
        if issuer_did in self._local_pins:
            return True
        try:
            rot = self._get("/ledger/rotations").get("rotations") or []
        except Exception:
            return False
        from .verify import verify_rotation_chain
        for pinned in self._local_pins:
            if verify_rotation_chain(pinned, issuer_did, rot):
                self._local_pins.append(issuer_did)
                return True
        return False

    def _verify_live(self, doc: dict[str, Any]) -> Optional[str]:
        """Full verification of a live signed envelope. Returns None when the
        document is acceptable, else a failure reason."""
        v = verify_data_integrity(doc)
        if not v["verified"]:
            return f"proof: {v['reason']}"
        valid, _age = within_validity(doc)
        if not valid:
            return "outside validity window"
        decision = doc.get("decision")
        if decision is not None:
            errs = validate_decision(decision)
            if errs:
                return "not AGD-1 conformant: " + "; ".join(errs[:4])
        errs = binding_violations(doc)
        if errs:
            return "counterparty binding violated: " + "; ".join(errs[:4])
        # Issuer acceptance LAST: it may establish a first-use pin, and an
        # invalid-window, non-conformant or mis-bound document must never
        # be the one that pins an issuer.
        if not self._issuer_allowed(v["issuer_did"] or ""):
            return f"issuer not allowed: {v['issuer_did']}"
        return None

    # -- decisions ------------------------------------------------------------
    def signed_decision(self, capability: str,
                        ttl_seconds: int = 3600) -> tuple[Optional[dict[str, Any]],
                                                          str, Optional[float]]:
        """-> (signed_envelope|None, channel, age_seconds).

        channel: "live" (fetched AND fully verified now), "cache" (served from
        the signed cache — every cache read re-verifies; envelope may be past
        valid_until, age says how old), "unverified" (the Guild answered but
        the document failed verification — treated as no evidence, never as
        live), "payment_required" (the Guild quoted a price for the signed
        decision and nothing verifiable was cached — see
        ``last_payment_required``; nothing was paid), or "outage" (nothing
        verifiable available)."""
        q = urllib.parse.urlencode({"capability": capability, "signed": "true",
                                    "ttl_seconds": ttl_seconds})
        fetched: Optional[dict[str, Any]] = None
        failure: Optional[str] = None
        quoted = False
        try:
            fetched = self._get(f"/check?{q}")
        except PaymentRequired:
            # The Guild priced the signed decision. Surfaced, never paid.
            fetched = None
            quoted = True
        except Exception:
            fetched = None
        if fetched is not None and not isinstance(fetched, dict):
            fetched = None
            failure = "response is not a JSON object"
        if fetched is not None:
            failure = self._verify_live(fetched)
            if failure is None:
                self.stats["live_fetches"] += 1
                if self.cache is not None:
                    self.cache.put("decision", capability, fetched)
                return fetched, "live", 0.0
            self.stats["live_verify_failures"] += 1
            self.last_verify_failure = failure
        if self.cache is not None:
            doc, state, age = self.cache.get("decision", capability)
            if doc is not None:
                self.stats["cache_serves"] += 1
                return doc, "cache", age
        self.stats["outages"] += 1
        if failure is not None:
            return None, "unverified", None
        return None, ("payment_required" if quoted else "outage"), None

    # -- passports -------------------------------------------------------------
    def passport_result(self, agent_id: str) -> PassportResult:
        """Fetch ``agent_id``'s Agent Passport and verify it BEFORE use.

        Accepted only when the credential (1) carries a valid eddsa-jcs-2022
        proof, (2) comes from an allowed/pinned issuer, (3) is inside its
        ``validFrom``/``validUntil`` window and (4) is about the identity that
        was asked for (a ``did:`` id must equal ``credentialSubject.id``; a
        Guild-local id is matched against the issuer's ``urn:passport:<id>:``
        credential id). Anything else is reported with a reason, never
        returned as a passport. Falls back to the signed cache when the Guild
        is unreachable; a cached credential is re-verified and re-bound."""
        path = f"/agents/{urllib.parse.quote(agent_id, safe='')}/passport"
        try:
            doc = self._get(path)
        except PaymentRequired as e:
            return self._cached_passport(agent_id, "payment_required", str(e))
        except GuildHTTPError as e:
            if e.status == 404:
                return self._cached_passport(agent_id, "not_found",
                                             "agent not found or no reputation")
            return self._cached_passport(agent_id, "outage", str(e))
        except Exception as e:
            return self._cached_passport(agent_id, "outage",
                                         f"{type(e).__name__}: {e}")
        if not isinstance(doc, dict):
            return self._cached_passport(agent_id, "unverified",
                                         "response is not a JSON object")
        reason, v_issuer, age = self._check_passport(agent_id, doc)
        subject = (doc.get("credentialSubject") or {}).get("id") \
            if isinstance(doc.get("credentialSubject"), dict) else None
        if reason is not None:
            self.stats["live_verify_failures"] += 1
            self.last_verify_failure = reason
            res = self._cached_passport(agent_id, "unverified", reason)
            res.subject_did = res.subject_did or subject
            return res
        self.stats["live_fetches"] += 1
        if self.cache is not None:
            self.cache.put("passport", agent_id, doc)
        return PassportResult(agent_id, doc, "live", None, "fresh", age,
                              subject, v_issuer)

    def _check_passport(self, agent_id: str, doc: dict[str, Any]
                        ) -> tuple[Optional[str], Optional[str], Optional[float]]:
        v = verify_data_integrity(doc)
        if not v["verified"]:
            return f"proof: {v['reason']}", None, None
        issuer = v["issuer_did"] or ""
        valid, age = within_validity(doc)
        if not valid:
            return "outside validity window", issuer, age
        sub = _passport_subject_ok(agent_id, doc)
        if sub is not None:
            return sub, issuer, age
        # issuer acceptance last: never pin from a stale or mis-bound credential
        if not self._issuer_allowed(issuer):
            return f"issuer not allowed: {issuer}", issuer, age
        return None, issuer, age

    def _cached_passport(self, agent_id: str, channel: str,
                         reason: Optional[str]) -> PassportResult:
        if self.cache is not None:
            doc, state, age = self.cache.get("passport", agent_id)
            if doc is not None:
                sub = _passport_subject_ok(agent_id, doc)
                if sub is None:
                    self.stats["cache_serves"] += 1
                    v = verify_data_integrity(doc)
                    return PassportResult(
                        agent_id, doc, "cache", reason, state, age,
                        (doc.get("credentialSubject") or {}).get("id"),
                        v.get("issuer_did"))
                reason = f"{reason}; cached credential rejected: {sub}"
        if channel in ("outage", "payment_required", "not_found"):
            self.stats["outages"] += 1
        return PassportResult(agent_id, None, channel, reason)

    def passport(self, agent_id: str) -> Optional[dict[str, Any]]:
        """Verified passport document for ``agent_id``, or None. See
        :meth:`passport_result` for the reason when None is returned."""
        return self.passport_result(agent_id).doc

    # -- free preflight ----------------------------------------------------------
    def preflight(self, url: str) -> PreflightResult:
        """FREE live preflight of an endpoint you are about to delegate to
        (``GET /preflight?url=``): separates what the endpoint claims from
        what it just proved. No key, no registration, no payment.

        This is an observation, not evidence: the answer is not signed and is
        not cached. Raises PaymentRequired if the service ever prices the
        route (it is documented free), GuildHTTPError for other failures, and
        the underlying urllib/socket error when the Guild is unreachable."""
        q = urllib.parse.urlencode({"url": url})
        doc = self._get(f"/preflight?{q}")
        if not isinstance(doc, dict) or "verdict" not in doc:
            raise GuildHTTPError(200, "/preflight", doc)
        return PreflightResult(
            target=str(doc.get("target") or url),
            verdict=str(doc.get("verdict")),
            headline=str(doc.get("headline") or ""),
            checks=list(doc.get("checks") or []),
            failed=list(doc.get("failed") or []),
            unknowns=list(doc.get("unknowns") or []),
            scored=list(doc.get("scored") or []),
            method=str(doc.get("method") or ""),
            limits=str(doc.get("limits") or ""),
            raw=doc,
        )

    # -- paid-quote discovery ------------------------------------------------------
    def quote(self, capability: str, signed: bool = True,
              ttl_seconds: int = 3600) -> dict[str, Any]:
        """Discover what a trust read costs WITHOUT paying.

        Performs the canonical ``GET /check`` request. If the service serves
        it (free tier, sandbox credits on ``api_key``, or a lab instance) the
        result is ``{"status": "served", "document": ...}``; if it prices the
        read the result is ``{"status": "payment_required", "quote": <402
        body>, "payment_required_header": <raw header or None>}``. Any other
        outcome is ``{"status": "error", "error": "..."}``. No payment is
        made, no retry with credentials is attempted."""
        q = urllib.parse.urlencode({"capability": capability,
                                    "signed": "true" if signed else "false",
                                    "ttl_seconds": ttl_seconds})
        try:
            doc = self._get(f"/check?{q}")
        except PaymentRequired as e:
            return {"status": "payment_required", "quote": e.quote,
                    "payment_required_header": e.payment_required_header,
                    "terms": e.terms,
                    "resource": f"{self.base}/check?{q}"}
        except Exception as e:
            return {"status": "error", "error": f"{type(e).__name__}: {e}"}
        return {"status": "served", "document": doc}

    # -- explicit registration (optional; never implicit) -----------------------
    def register(self, name: str, capabilities: list[str], *,
                 metadata: Optional[dict[str, Any]] = None,
                 public_key: Optional[str] = None,
                 config: Optional[dict[str, Any]] = None,
                 principal: Optional[str] = None,
                 referred_by: Optional[str] = None,
                 src: Optional[str] = REGISTER_SRC) -> dict[str, Any]:
        """Register an agent with the Guild (``POST /agents/register``) — ONLY
        when the caller asks for it. Nothing in this library registers on its
        own, and registration is not needed for :meth:`preflight`,
        :meth:`passport_result` or :meth:`quote`.

        Returns the Guild's RegisterResponse unchanged: ``id``, ``did``,
        ``public_key``, ``custodial``, and — for a custodial identity — a
        one-time ``api_key``. The key is returned to you and to nothing
        else: the client does not store, adopt or log it. Pass it explicitly
        (``GuildClient(api_key=...)``) if you want authenticated calls.

        Never sends ``seed`` or any admin/first-party header on its own;
        supply ``public_key`` (ed25519, hex) for a self-sovereign identity.
        ``src`` is an honest attribution tag for the surface used (defaults to
        this package's name); pass ``src=None`` to omit it."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a non-empty string")
        if not isinstance(capabilities, list) or \
           not all(isinstance(c, str) and c for c in capabilities):
            raise ValueError("capabilities must be a list of non-empty strings")
        if src is not None and not _SRC_RE.match(src):
            raise ValueError("src must match ^[a-z0-9_:-]{1,64}$")
        body: dict[str, Any] = {"name": name, "capabilities": list(capabilities),
                                "metadata": dict(metadata or {})}
        if public_key is not None:
            body["public_key"] = public_key
        if config is not None:
            body["config"] = config
        if principal is not None:
            body["principal"] = principal
        if referred_by is not None:
            body["referred_by"] = referred_by
        if src is not None:
            body["src"] = src
        doc = self._post("/agents/register", body)
        if not isinstance(doc, dict) or not doc.get("id") or not doc.get("did"):
            raise GuildHTTPError(200, "/agents/register", doc)
        return doc

    # -- outcome reporting -----------------------------------------------------
    def post_signed_outcome(self, doc: dict[str, Any]) -> Optional[dict[str, Any]]:
        try:
            return self._post("/outcomes", doc)
        except Exception:
            return None

    def ledger_record(self, record_id: str) -> Optional[dict[str, Any]]:
        try:
            return self._get(f"/ledger/record/{urllib.parse.quote(record_id)}"
                             ).get("record")
        except Exception:
            return None

    def record_collaboration(self, body: dict[str, Any]) -> Optional[dict[str, Any]]:
        try:
            return self._post("/collaborations", body)
        except Exception:
            return None
