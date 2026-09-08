"""Reachability semantics + SSRF-safe declaration-time verifier.

Single source of truth for how the Guild talks about whether a provider can be
contacted, and the ONLY place a liveness check may run. Read paths
(/check, /search, listings, journey, dashboard, demand) call the PURE
reachability_fields()/status_for() and never touch the network.

Formal definitions + status-transition table: REACHABILITY_SEMANTICS.md.

Evidence ladder (weakest → strongest), with routing eligibility:
  no_endpoint            evidence=none            route=NO
  unknown                evidence=none            route=NO   (malformed/policy-invalid)
  declared_unverified    evidence=none            route=NO   (agent's claim only)
  verification_inconclusive evidence=none         route=NO   (probe couldn't decide)
  http_responsive        evidence=http_response   route=NO   (a server answered — 401/403/
                                                              404/405 all count — but NO
                                                              protocol proof; NOT routable)
  currently_unreachable  evidence=none            route=NO   (last probe failed)
  recently_reachable     evidence=protocol_handshake route=YES (protocol-specific success:
                                                              A2A card / MCP initialise /
                                                              declared health route)
  invocation_verified    evidence=guild_invocation route=YES (a trusted AG-ORIGINATED
                                                              invocation to the CURRENT
                                                              endpoint returned a successful
                                                              protocol response, bound by a
                                                              unique invocation id)

A weak HTTP response NEVER inherits the routing recommendation of a protocol
handshake. INVOCATION_VERIFIED is NEVER inferred from a submitted receipt.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import ssl
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

# --- statuses / evidence -----------------------------------------------------
NO_ROUTE_STATUSES = ("no_endpoint", "unknown", "declared_unverified",
                     "verification_inconclusive", "http_responsive",
                     "currently_unreachable")
ROUTABLE_STATUSES = ("recently_reachable", "invocation_verified")
VERIFIED_STATUSES = ("http_responsive", "recently_reachable",
                     "currently_unreachable", "invocation_verified",
                     "verification_inconclusive")

EVIDENCE_LEVELS = ("none", "http_response", "protocol_handshake", "guild_invocation")

# Internal probe OUTCOMES (kept distinct from stored statuses, per refinement).
OUTCOME_NETWORK_REACHABLE = "network_reachable"
OUTCOME_HTTP_RESPONSIVE = "http_responsive"
OUTCOME_PROTOCOL_RESPONSIVE = "protocol_responsive"
OUTCOME_UNREACHABLE = "currently_unreachable"
OUTCOME_INCONCLUSIVE = "verification_inconclusive"
EXECUTION_OBSERVATION_VERSION = "execution-routing-v1"


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, str(default)))
    except ValueError:
        v = default
    return max(lo, min(v, hi))


# --- configurable TTLs (bounded) ---------------------------------------------
def recent_ttl() -> int:      # protocol handshake freshness
    return _env_int("GUILD_REACH_RECENT_TTL", 24 * 3600, 300, 30 * 24 * 3600)


def http_ttl() -> int:        # weak http-responsive freshness
    return _env_int("GUILD_REACH_HTTP_TTL", 6 * 3600, 300, 30 * 24 * 3600)


def unreach_ttl() -> int:
    return _env_int("GUILD_REACH_UNREACH_TTL", 24 * 3600, 300, 30 * 24 * 3600)


def invocation_ttl() -> int:
    return _env_int("GUILD_REACH_INVOCATION_TTL", 7 * 24 * 3600, 300, 90 * 24 * 3600)


def _ttl_for(status: str) -> int:
    return {"recently_reachable": recent_ttl(), "http_responsive": http_ttl(),
            "currently_unreachable": unreach_ttl(),
            "verification_inconclusive": http_ttl(),
            "invocation_verified": invocation_ttl()}.get(status, recent_ttl())


ALLOWED_PORTS = {80, 443, 8080, 8443}
PROBE_TIMEOUT_S = 3.0
PROBE_MAX_BYTES = 8192

# concurrency: cap outbound probes so one agent can't exhaust workers
_MAX_CONCURRENT = _env_int("GUILD_REACH_MAX_PROBES", 4, 1, 32)
_probe_sem = threading.BoundedSemaphore(_MAX_CONCURRENT)
_inflight_lock = threading.Lock()
_inflight: set[str] = set()   # dedup identical (agent|endpoint) verifications


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def endpoint_fingerprint(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    return "epf_" + hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:16]


# --- 1. URL POLICY (pure) ----------------------------------------------------
def url_policy_check(url: str) -> tuple[bool, str]:
    if not url or len(url) > 500:
        return False, "empty or over-long url"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False, f"unsupported scheme {parts.scheme!r} (http/https only)"
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        return False, "embedded credentials are not allowed in the endpoint"
    if not parts.hostname:
        return False, "missing host"
    if parts.port is not None and parts.port not in ALLOWED_PORTS:
        return False, f"port {parts.port} not permitted"
    try:
        ip = ipaddress.ip_address(parts.hostname)
        ok, reason = _screen_ip(ip)
        if not ok:
            return False, reason
    except ValueError:
        pass
    return True, "ok"


def _screen_ip(ip) -> tuple[bool, str]:
    if ip.is_loopback:
        return False, "loopback address"
    if ip.is_private:
        return False, "private address space"
    if ip.is_link_local:
        return False, "link-local address"
    if ip.is_multicast:
        return False, "multicast address"
    if ip.is_unspecified:
        return False, "unspecified address"
    if ip.is_reserved:
        return False, "reserved address"
    return True, "ok"


def _resolve_and_screen(host: str, port: int) -> tuple[bool, list[tuple[int, str]], str]:
    """Resolve host, screen EVERY address (IPv4 and IPv6). Returns
    [(family, addr), ...] of screened-public addresses."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return False, [], f"dns resolution failed: {e}"
    addrs = []
    for info in infos:
        family, addr = info[0], info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False, [], f"unparseable resolved address {addr!r}"
        ok, reason = _screen_ip(ip)
        if not ok:
            return False, [], f"dns resolves to {reason} ({addr})"
        addrs.append((family, addr))
    if not addrs:
        return False, [], "no addresses resolved"
    return True, addrs, "ok"


# --- 2. PINNED TRANSPORT -----------------------------------------------------
def _connect_pinned(scheme: str, host: str, family: int, addr: str, port: int,
                    ssl_context: Optional[ssl.SSLContext] = None):
    """Open a socket to the PINNED, already-screened address. For https, wrap
    with TLS using SNI=host and full certificate+hostname validation against
    the ORIGINAL hostname (never the IP). The address is fixed here, so the
    HTTP layer can never re-resolve the hostname (DNS-rebinding safe). TLS
    verification is NEVER disabled."""
    raw = socket.socket(family, socket.SOCK_STREAM)
    raw.settimeout(PROBE_TIMEOUT_S)
    raw.connect((addr, port))
    if scheme == "https":
        ctx = ssl_context or ssl.create_default_context()
        # explicit belt-and-braces: default context already sets these
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        # SNI + cert hostname validation both use `host`, not `addr`
        return ctx.wrap_socket(raw, server_hostname=host)
    return raw


def _dechunk(raw: bytes) -> bytes:
    """Decode an HTTP/1.1 chunked body, tolerating truncation.

    The probe caps its read at PROBE_MAX_BYTES, so the last chunk is routinely
    incomplete and the terminating 0-chunk is usually never seen. That is
    fine: we return everything decoded so far, because the callers only need
    enough of the body to recognise a card or a handshake. Returns the input
    unchanged if it does not look chunked, so a mislabelled response degrades
    to the previous behaviour rather than to an empty body."""
    out = bytearray()
    pos = 0
    n = len(raw)
    while pos < n:
        eol = raw.find(b"\r\n", pos)
        if eol == -1:
            break
        size_line = raw[pos:eol].split(b";", 1)[0].strip()
        try:
            size = int(size_line, 16)
        except ValueError:
            return raw if not out else bytes(out)
        if size == 0:
            break
        start = eol + 2
        out += raw[start:start + size]
        pos = start + size + 2      # skip the chunk's trailing CRLF
    return bytes(out) if out else raw


def _http_request_pinned(scheme: str, host: str, family: int, addr: str,
                         port: int, path: str, method: str = "HEAD",
                         body: Optional[bytes] = None,
                         extra_headers: str = "",
                         ssl_context: Optional[ssl.SSLContext] = None
                         ) -> tuple[Optional[int], bytes]:
    """One bounded HTTP request over a pinned connection. Returns
    (status_code, body_prefix). No redirects are followed (caller treats 3xx as
    a failure). Body read is capped at PROBE_MAX_BYTES; not otherwise processed.
    No credentials are ever sent."""
    sock = _connect_pinned(scheme, host, family, addr, port, ssl_context)
    try:
        # Header lookup in ASGI servers may use the first value. Sending the
        # default */* before MCP's required JSON/SSE Accept caused HTTP 406.
        accept = "" if any(line.lower().startswith("accept:")
                           for line in extra_headers.splitlines()) else "Accept: */*\r\n"
        req = (f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n"
               f"User-Agent: guild-reachability-probe/1\r\n"
               f"{accept}Connection: close\r\n{extra_headers}")
        if body is not None:
            req += f"Content-Length: {len(body)}\r\n"
        req += "\r\n"
        sock.sendall(req.encode("ascii", "ignore") + (body or b""))
        sock.settimeout(PROBE_TIMEOUT_S)
        buf = b""
        while len(buf) < PROBE_MAX_BYTES:
            try:
                chunk = sock.recv(min(1024, PROBE_MAX_BYTES - len(buf)))
            except socket.timeout:
                # SSE can keep the connection open after a complete reply.
                # Preserve observed bytes; the protocol parser still requires
                # a complete, matching result before granting protocol proof.
                if not buf:
                    raise
                break
            if not chunk:
                break
            buf += chunk
        first = buf.split(b"\r\n", 1)[0].decode("ascii", "ignore")
        bits = first.split(" ")
        code = None
        if len(bits) >= 2 and bits[0].startswith("HTTP/"):
            try:
                code = int(bits[1])
            except ValueError:
                code = None
        head, _, body_prefix = buf.partition(b"\r\n\r\n")
        if b"\r\n\r\n" not in buf:
            body_prefix = b""
        # CHUNKED TRANSFER-ENCODING (defect found 2026-07-31). The raw body of
        # a chunked response begins with a hex chunk LENGTH line, so every
        # JSON check downstream failed to parse it. Because the A2A card check
        # (_looks_like_a2a_card) is what promotes an endpoint from
        # "http_responsive" to "recently_reachable / protocol_handshake", ANY
        # agent served over chunked encoding — which is the default for most
        # streaming frameworks, including our own — was being classified as
        # unproven. That is why `verified_reachable` read 0 for every entry in
        # the demand feed: not because nobody was reachable, but because the
        # prober could not read them. Undercounting reachability is the exact
        # error class this service exists to eliminate, so it is fixed here.
        if b"transfer-encoding: chunked" in head.lower():
            body_prefix = _dechunk(body_prefix)
        return code, body_prefix
    finally:
        try:
            sock.close()
        except Exception:
            pass


# --- 3. LIVENESS PROBE (owner-initiated, SSRF-safe) --------------------------
def _probe_record(*args, **kwargs) -> dict[str, Any]:
    record = make_record(*args, **kwargs)
    record["execution_observation_version"] = EXECUTION_OBSERVATION_VERSION
    return record


def _execution_declaration(body: bytes, endpoint: str) -> Optional[dict]:
    """Parse data from the exact endpoint's complete card, never instructions.

    A claim is a provider declaration, not proof of competence or completion.
    Truncated JSON and a card for another endpoint cannot supply this evidence.
    Only a typed, versioned boolean is accepted; remote reasons are not copied.
    """
    try:
        card = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(card, dict) or card.get("url") != endpoint:
        return None
    guild = card.get("agentGuild")
    claims = [("execution", card.get("execution"))]
    if isinstance(guild, dict):
        claims.append(("agentGuild.execution", guild.get("execution")))
    valid = [(source, claim) for source, claim in claims
             if isinstance(claim, dict)
             and claim.get("version") == "worker-execution-v1"
             and type(claim.get("accepting_work")) is bool]
    if not valid:
        return None
    # A conflicting explicit refusal wins; never choose a permissive alias.
    source, claim = min(valid, key=lambda entry: entry[1]["accepting_work"])
    now = _now()
    return {
        "accepting_work": claim["accepting_work"],
        "source": source, "evidence": "provider_declaration",
        "checked_at": _iso(now),
        "expires_at": _iso(now + timedelta(seconds=recent_ttl())),
        "endpoint_fingerprint": endpoint_fingerprint(endpoint),
    }


def preserve_execution_observation(previous: Optional[dict], record: dict) -> dict:
    """Contact, omission and redeclaration do not retract an observed refusal.

    Preserve its original age rather than refreshing it without new evidence.
    A valid new declaration supersedes it; an endpoint change invalidates it.
    """
    result = dict(record)
    old = (previous or {}).get("execution_availability")
    if (not result.get("execution_availability") and isinstance(old, dict)
            and old.get("endpoint_fingerprint") == result.get("endpoint_fingerprint")):
        result["execution_availability"] = dict(old)
    if ("execution_observation_version" not in result and previous
            and previous.get("endpoint_fingerprint") == result.get("endpoint_fingerprint")
            and previous.get("execution_observation_version")):
        result["execution_observation_version"] = previous["execution_observation_version"]
    return result


def execution_fields(endpoint: Optional[str], record: Optional[dict]) -> dict:
    observation = (record or {}).get("execution_availability")
    if (not isinstance(observation, dict)
            or observation.get("endpoint_fingerprint") != endpoint_fingerprint(endpoint)
            or type(observation.get("accepting_work")) is not bool):
        return {"status": "unknown", "accepting_work": None,
                "evidence": "no_endpoint_bound_declaration"}
    stale = _expired(observation)
    accepting = observation["accepting_work"]
    return {**observation, "stale": stale,
            "status": ("stale_" if stale else "")
                      + ("declared_available" if accepting else "unavailable")}


def liveness_probe(url: str, *, ssl_context: Optional[ssl.SSLContext] = None
                   ) -> dict[str, Any]:
    """A single bounded SSRF-safe check. Chooses a protocol-specific probe when
    the endpoint declares one (A2A card / MCP initialise), else a generic HTTP
    fallback. NEVER raises; NEVER sends a task, credential or sensitive payload.
    Returns a reachability record (see make_record)."""
    ok, reason = url_policy_check(url)
    if not ok:
        return _probe_record("currently_unreachable", "declaration_probe",
                           "none", url, detail=f"policy: {reason}")
    parts = urlsplit(url)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    ok, addrs, reason = _resolve_and_screen(host, port)
    if not ok:
        return _probe_record("currently_unreachable", "declaration_probe",
                           "none", url, detail=reason)
    family, addr = addrs[0]

    def _req(path, method="HEAD", body=None, headers=""):
        return _http_request_pinned(parts.scheme, host, family, addr, port,
                                    path, method, body, headers, ssl_context)

    observations: dict[str, Any] = {}
    try:
        outcome, code, detail = _classify(parts, _req, observations)
    except ssl.SSLError as e:
        return _probe_record("currently_unreachable", "declaration_probe",
                           "none", url, detail=f"tls failure: {type(e).__name__}")
    except Exception as e:
        return _probe_record("currently_unreachable", "declaration_probe",
                           "none", url, detail=f"probe failed: {type(e).__name__}")

    if outcome == OUTCOME_PROTOCOL_RESPONSIVE:
        record = _probe_record("recently_reachable", "protocol_probe",
                               "protocol_handshake", url, detail=detail)
        if observations.get("execution_availability"):
            record["execution_availability"] = observations["execution_availability"]
        return record
    if outcome == OUTCOME_HTTP_RESPONSIVE:
        # a server answered but proved no protocol — weak evidence, NOT routable
        record = _probe_record("http_responsive", "declaration_probe",
                               "http_response", url, detail=detail)
        if observations.get("protocol_probe"):
            record["protocol_probe"] = observations["protocol_probe"]
        return record
    if outcome == OUTCOME_UNREACHABLE:
        return _probe_record("currently_unreachable", "declaration_probe",
                           "none", url, detail=detail)
    return _probe_record("verification_inconclusive", "declaration_probe",
                       "none", url, detail=detail)


def _mcp_initialize_result(body: bytes) -> bool:
    """Recognize a complete matching InitializeResult in JSON or SSE.

    Text mentioning jsonrpc/result, error replies, unrelated request IDs and
    incomplete prefixes are not evidence of a successful initialization.
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    candidates = [text]
    data: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line:
            if data:
                candidates.append("\n".join(data))
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    for candidate in candidates:
        try:
            msg = json.loads(candidate)
        except (ValueError, UnicodeDecodeError):
            continue
        if (not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0"
                or type(msg.get("id")) is not int or msg["id"] != 1
                or "error" in msg):
            continue
        result = msg.get("result")
        if not isinstance(result, dict):
            continue
        info = result.get("serverInfo")
        if (isinstance(result.get("protocolVersion"), str)
                and result["protocolVersion"] in {
                "2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
                and isinstance(result.get("capabilities"), dict)
                and isinstance(info, dict)
                and all(isinstance(info.get(k), str) and info[k].strip()
                        for k in ("name", "version"))):
            return True
    return False


def _classify(parts, req, observations: Optional[dict] = None
              ) -> tuple[str, Optional[int], str]:
    """Return (outcome, http_code, detail). Protocol-specific first."""
    path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
    # A2A: an Agent Card at the well-known path with card markers = protocol proof
    if "/a2a" in parts.path or parts.path in ("", "/"):
        code, body = req("/.well-known/agent-card.json", method="GET")
        if code and 200 <= code < 300 and _looks_like_a2a_card(body):
            if observations is not None:
                observations["execution_availability"] = _execution_declaration(
                    body, parts.geturl())
            return OUTCOME_PROTOCOL_RESPONSIVE, code, "a2a agent-card handshake"
    # MCP: initialise handshake (no secrets) with a jsonrpc result = protocol proof
    if "/mcp" in parts.path:
        init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-03-26",
                                      "capabilities": {},
                                      "clientInfo": {"name": "guild-probe",
                                                     "version": "1"}}}).encode()
        code, body = req(path, method="POST", body=init,
                         headers="Content-Type: application/json\r\n"
                                 "Accept: application/json, text/event-stream\r\n")
        if code and 200 <= code < 300 and _mcp_initialize_result(body):
            return OUTCOME_PROTOCOL_RESPONSIVE, code, "mcp initialise handshake"
        if code in (401, 402, 403):
            if observations is not None:
                observations["protocol_probe"] = {
                    "protocol": "mcp", "result": "authorization_required",
                    "http_status": code}
            return OUTCOME_HTTP_RESPONSIVE, code, f"MCP initialize requires authorization (HTTP {code})"
        # The bounded reader may receive only part of a large JSON/SSE reply.
        # That cannot prove the protocol, but is not evidence it is broken.
        text = body.decode("utf-8", "replace").strip()
        if code and 200 <= code < 300 and (text.startswith(("{", "event:", "data:", ":"))):
            if observations is not None:
                observations["protocol_probe"] = {
                    "protocol": "mcp", "result": "inconclusive",
                    "http_status": code}
            return OUTCOME_HTTP_RESPONSIVE, code, "MCP response did not contain a complete matching initialization result"
    # Generic fallback: HEAD, then GET if HEAD is not allowed (405).
    code, _ = req(path, method="HEAD")
    if code is None:
        return OUTCOME_UNREACHABLE, None, "no HTTP status line"
    if 300 <= code < 400:
        return OUTCOME_UNREACHABLE, code, f"redirect {code} refused"
    if code == 405:
        code2, _ = req(path, method="GET")
        if code2 and 200 <= code2 < 400 and not (300 <= code2 < 400):
            return OUTCOME_HTTP_RESPONSIVE, code2, f"http {code2} (HEAD 405)"
        if code2 and 400 <= code2 < 500:
            return OUTCOME_HTTP_RESPONSIVE, code2, f"http {code2}"
        return OUTCOME_INCONCLUSIVE, code2, "HEAD 405, GET inconclusive"
    if 200 <= code < 500:
        # a server clearly responded (incl 401/403/404) — weak, not protocol
        return OUTCOME_HTTP_RESPONSIVE, code, f"http {code}"
    return OUTCOME_UNREACHABLE, code, f"http {code}"


def _looks_like_a2a_card(body: bytes) -> bool:
    """Does this response body look like an A2A Agent Card?

    TRUNCATION TOLERANCE (defect found 2026-07-31). The probe read is bounded,
    so a LARGE but perfectly valid card arrives incomplete and json.loads
    fails on it. The strict-parse version of this function therefore reported
    well-formed agents as unproven purely for being verbose — our own card is
    one of them. Undercounting reachability is the same error class as
    overcounting adoption, so: parse when we can, and otherwise fall back to
    the marker keys, which cannot appear in a non-JSON error page."""
    text = body.decode("utf-8", "ignore")
    try:
        d = json.loads(text)
        return isinstance(d, dict) and ("skills" in d or "protocolVersion" in d)
    except Exception:
        pass
    head = text.lstrip()[:1]
    if head != "{":
        return False
    return ('"protocolVersion"' in text) or ('"skills"' in text)


# --- 4. INVOCATION VERIFICATION (trusted, AG-originated only) -----------------
def invocation_verified_record(url: str, invocation_id: str) -> dict[str, Any]:
    """The ONLY producer of invocation_verified. Callers (store.complete_
    outbound_invocation) MUST have already checked: AG initiated the invocation,
    it targeted the CURRENT endpoint (fingerprint match), a unique invocation id
    bound it, and the endpoint returned a successful protocol response. Never
    produced from a submitted receipt or an agent-supplied claim."""
    rec = make_record("invocation_verified", "guild_originated_invocation",
                      "guild_invocation", url,
                      detail=f"AG-originated invocation {invocation_id} succeeded")
    rec["invocation_id"] = invocation_id
    return rec


# --- 5. RECORD + EFFECTIVE STATUS (pure) -------------------------------------
def make_record(status: str, method: str, evidence_level: str,
                endpoint: str, detail: str = "") -> dict[str, Any]:
    now = _now()
    verified = status in ("recently_reachable", "invocation_verified")
    return {
        "status": status,
        "evidence_level": evidence_level,
        "method": method,
        "checked_at": _iso(now),
        "last_verified_at": _iso(now) if verified else None,
        "expires_at": _iso(now + timedelta(seconds=_ttl_for(status))),
        "endpoint_fingerprint": endpoint_fingerprint(endpoint),
        "detail": detail,
    }


def _expired(record: dict) -> bool:
    exp = record.get("expires_at")
    if not exp:
        return True
    try:
        return _now() >= datetime.fromisoformat(exp)
    except ValueError:
        return True


def status_for(endpoint: Optional[str], record: Optional[dict] = None) -> str:
    if not endpoint:
        return "no_endpoint"
    ok, _ = url_policy_check(str(endpoint))
    if not ok:
        return "unknown"
    if record:
        # endpoint change invalidates all prior evidence for the old endpoint
        if record.get("endpoint_fingerprint") != endpoint_fingerprint(endpoint):
            return "declared_unverified"
        if not _expired(record) and record.get("status") in VERIFIED_STATUSES:
            return record["status"]
    return "declared_unverified"


def reachability_fields(endpoint: Optional[str],
                        record: Optional[dict] = None) -> dict[str, Any]:
    """Pure read-path field set. Applies fingerprint invalidation + TTL expiry.
    Never touches the network."""
    status = status_for(endpoint, record)
    declared = status != "no_endpoint" and status != "unknown"
    use_rec = bool(record
                   and record.get("endpoint_fingerprint") == endpoint_fingerprint(endpoint)
                   and status == record.get("status"))
    execution = execution_fields(endpoint, record)
    return {
        "has_declared_endpoint": declared,
        "reachability_status": status,
        "evidence_level": (record.get("evidence_level") if use_rec
                           else ("none" if status != "declared_unverified" else "none")),
        "verification_method": (record.get("method") if use_rec
                                else ("declaration_only" if status == "declared_unverified"
                                      else None)),
        "last_verified_at": record.get("last_verified_at") if use_rec else None,
        "verification_age_seconds": _age(record.get("checked_at")) if use_rec else None,
        "expires_at": record.get("expires_at") if use_rec else None,
        "endpoint_fingerprint": endpoint_fingerprint(endpoint),
        # only a successful AG-originated invocation proves work goes through
        "invocation_supported": status == "invocation_verified",
        "execution_availability": execution,
        "recommended_for_routing": (status in ROUTABLE_STATUSES
                                    and execution["accepting_work"] is not False),
    }


def _age(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        return int((_now() - datetime.fromisoformat(iso)).total_seconds())
    except ValueError:
        return None
