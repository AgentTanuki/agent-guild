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
        req = (f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n"
               f"User-Agent: guild-reachability-probe/1\r\n"
               f"Accept: */*\r\nConnection: close\r\n{extra_headers}")
        if body is not None:
            req += f"Content-Length: {len(body)}\r\n"
        req += "\r\n"
        sock.sendall(req.encode("ascii", "ignore") + (body or b""))
        sock.settimeout(PROBE_TIMEOUT_S)
        buf = b""
        while len(buf) < PROBE_MAX_BYTES:
            chunk = sock.recv(min(1024, PROBE_MAX_BYTES - len(buf)))
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
        body_prefix = buf.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in buf else b""
        return code, body_prefix
    finally:
        try:
            sock.close()
        except Exception:
            pass


# --- 3. LIVENESS PROBE (owner-initiated, SSRF-safe) --------------------------
def liveness_probe(url: str, *, ssl_context: Optional[ssl.SSLContext] = None
                   ) -> dict[str, Any]:
    """A single bounded SSRF-safe check. Chooses a protocol-specific probe when
    the endpoint declares one (A2A card / MCP initialise), else a generic HTTP
    fallback. NEVER raises; NEVER sends a task, credential or sensitive payload.
    Returns a reachability record (see make_record)."""
    ok, reason = url_policy_check(url)
    if not ok:
        return make_record("currently_unreachable", "declaration_probe",
                           "none", url, detail=f"policy: {reason}")
    parts = urlsplit(url)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    ok, addrs, reason = _resolve_and_screen(host, port)
    if not ok:
        return make_record("currently_unreachable", "declaration_probe",
                           "none", url, detail=reason)
    family, addr = addrs[0]

    def _req(path, method="HEAD", body=None, headers=""):
        return _http_request_pinned(parts.scheme, host, family, addr, port,
                                    path, method, body, headers, ssl_context)

    try:
        outcome, code, detail = _classify(parts, _req)
    except ssl.SSLError as e:
        return make_record("currently_unreachable", "declaration_probe",
                           "none", url, detail=f"tls failure: {type(e).__name__}")
    except Exception as e:
        return make_record("currently_unreachable", "declaration_probe",
                           "none", url, detail=f"probe failed: {type(e).__name__}")

    if outcome == OUTCOME_PROTOCOL_RESPONSIVE:
        return make_record("recently_reachable", "protocol_probe",
                           "protocol_handshake", url, detail=detail)
    if outcome == OUTCOME_HTTP_RESPONSIVE:
        # a server answered but proved no protocol — weak evidence, NOT routable
        return make_record("http_responsive", "declaration_probe",
                           "http_response", url, detail=detail)
    if outcome == OUTCOME_UNREACHABLE:
        return make_record("currently_unreachable", "declaration_probe",
                           "none", url, detail=detail)
    return make_record("verification_inconclusive", "declaration_probe",
                       "none", url, detail=detail)


def _classify(parts, req) -> tuple[str, Optional[int], str]:
    """Return (outcome, http_code, detail). Protocol-specific first."""
    path = parts.path or "/"
    base = ""  # same host, path swapped
    # A2A: an Agent Card at the well-known path with card markers = protocol proof
    if "/a2a" in path or path in ("", "/"):
        code, body = req("/.well-known/agent-card.json", method="GET")
        if code and 200 <= code < 300 and _looks_like_a2a_card(body):
            return OUTCOME_PROTOCOL_RESPONSIVE, code, "a2a agent-card handshake"
    # MCP: initialise handshake (no secrets) with a jsonrpc result = protocol proof
    if "/mcp" in path:
        init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-03-26",
                                      "capabilities": {},
                                      "clientInfo": {"name": "guild-probe",
                                                     "version": "1"}}}).encode()
        code, body = req(path, method="POST", body=init,
                         headers="Content-Type: application/json\r\n"
                                 "Accept: application/json, text/event-stream\r\n")
        if code and 200 <= code < 300 and b"jsonrpc" in body and b"result" in body:
            return OUTCOME_PROTOCOL_RESPONSIVE, code, "mcp initialise handshake"
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
    try:
        d = json.loads(body.decode("utf-8", "ignore"))
    except Exception:
        return False
    return isinstance(d, dict) and ("skills" in d or "protocolVersion" in d)


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
        "recommended_for_routing": status in ROUTABLE_STATUSES,
    }


def _age(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        return int((_now() - datetime.fromisoformat(iso)).total_seconds())
    except ValueError:
        return None
