"""Reachability semantics + SSRF-safe declaration-time verifier — the single
source of truth for how the Guild talks about whether a provider can be
contacted, and the ONLY place a liveness check may run.

Formal definitions: docs/discovery-swarm/REACHABILITY_SEMANTICS.md.

Status ladder:
  no_endpoint          — never declared an endpoint.
  unknown              — a string is on file but malformed / policy-invalid.
  declared_unverified  — a well-formed public URL is on file. A CLAIM by the
                         agent, verified by nobody. Never called "reachable".
  recently_reachable   — an SSRF-safe declaration-time liveness check answered
                         within the last RECENT_TTL. Says the URL responds to
                         HTTP; says NOTHING about invocation semantics.
  currently_unreachable— the last verification attempt failed (connect/timeout/
                         redirect/error). The DECLARATION is preserved; only the
                         status reflects the failure. Expires to
                         declared_unverified after UNREACH_TTL.
  invocation_verified  — a guild-observed task receipt travelled THROUGH this
                         endpoint within INVOCATION_TTL. The only status that
                         proves the endpoint does WORK. Never set by a generic
                         HTTP response.

Verification is OWNER-INITIATED, at declaration time only. It must NEVER run
from /check, /search, capability listing, journey reads, dashboard reads,
background demand matching, or any routing read — those paths call
reachability_fields(), which is pure and never touches the network.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

PRODUCIBLE_STATUSES = ("no_endpoint", "declared_unverified", "unknown")
VERIFIED_STATUSES = ("recently_reachable", "currently_unreachable",
                     "invocation_verified")
ROUTABLE_STATUSES = ("recently_reachable", "invocation_verified")

# TTLs (seconds)
RECENT_TTL = 24 * 3600
UNREACH_TTL = 24 * 3600
INVOCATION_TTL = 7 * 24 * 3600

# Ports an agent endpoint may use. Everything else is refused at declaration.
ALLOWED_PORTS = {80, 443, 8080, 8443}
PROBE_TIMEOUT_S = 3.0          # a verifier timeout must not hold a worker long
PROBE_MAX_BYTES = 4096         # bound response bytes; we do not process the body


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- 1. URL POLICY (pure, no network) — gates DECLARATION --------------------

def url_policy_check(url: str) -> tuple[bool, str]:
    """Validate an endpoint URL's PROPERTIES only (no DNS, no network). Returns
    (ok, reason). A declaration is rejected iff this fails — never because a
    remote service is merely down."""
    if not url or len(url) > 500:
        return False, "empty or over-long url"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False, f"unsupported scheme {parts.scheme!r} (http/https only)"
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        return False, "embedded credentials are not allowed in the endpoint"
    host = parts.hostname
    if not host:
        return False, "missing host"
    if parts.port is not None and parts.port not in ALLOWED_PORTS:
        return False, f"port {parts.port} not permitted"
    # If the host is a literal IP, screen it now (no DNS needed).
    try:
        ip = ipaddress.ip_address(host)
        ok, reason = _screen_ip(ip)
        if not ok:
            return False, reason
    except ValueError:
        pass  # a hostname — screened at probe time after DNS resolution
    return True, "ok"


def _screen_ip(ip: "ipaddress._BaseAddress") -> tuple[bool, str]:
    """Reject any address that must not be contacted."""
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


def _resolve_and_screen(host: str, port: int) -> tuple[bool, list[str], str]:
    """Resolve host and screen EVERY returned address. All must be public."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return False, [], f"dns resolution failed: {e}"
    addrs = []
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False, [], f"unparseable resolved address {addr!r}"
        ok, reason = _screen_ip(ip)
        if not ok:
            return False, [], f"dns resolves to {reason} ({addr})"
        addrs.append(addr)
    if not addrs:
        return False, [], "no addresses resolved"
    return True, addrs, "ok"


# --- 2. LIVENESS PROBE (network) — SSRF-safe, owner-initiated only -----------

def liveness_probe(url: str) -> dict[str, Any]:
    """A single, bounded, SSRF-safe liveness check. Returns a verification
    record: {status, method, checked_at, last_verified_at, detail}. NEVER
    raises; a failure yields currently_unreachable with the declaration intact.

    DNS-rebinding defense: resolve, screen EVERY address, then connect to a
    PINNED screened address and send the Host header explicitly (the socket is
    bound to the address we validated, not re-resolved by the HTTP client). No
    redirects are followed (a 3xx is a verification failure). Response bytes are
    bounded and the body is not processed. No AG secret is ever sent."""
    ok, reason = url_policy_check(url)
    if not ok:
        return _rec("currently_unreachable", "declaration_probe",
                    detail=f"policy: {reason}")
    parts = urlsplit(url)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    ok, addrs, reason = _resolve_and_screen(host, port)
    if not ok:
        return _rec("currently_unreachable", "declaration_probe", detail=reason)
    pinned = addrs[0]  # connect to the exact address we screened
    try:
        status_line = _http_head_pinned(parts.scheme, host, pinned, port,
                                         parts.path or "/")
    except Exception as e:
        return _rec("currently_unreachable", "declaration_probe",
                    detail=f"probe failed: {type(e).__name__}")
    code = status_line
    if code is None:
        return _rec("currently_unreachable", "declaration_probe",
                    detail="no HTTP status line")
    if 300 <= code < 400:
        # redirects are refused as verification failures (rebinding vector)
        return _rec("currently_unreachable", "declaration_probe",
                    detail=f"redirect {code} refused")
    if 200 <= code < 500:
        # any non-redirect answer (incl. 401/404) proves the host is LIVE and
        # speaking HTTP — which is all recently_reachable claims.
        return _rec("recently_reachable", "declaration_probe",
                    detail=f"http {code}")
    return _rec("currently_unreachable", "declaration_probe",
                detail=f"http {code}")


def _http_head_pinned(scheme: str, host: str, addr: str, port: int,
                      path: str) -> Optional[int]:
    """Minimal HTTP: connect to a PINNED validated address, send HEAD with the
    real Host header, read only the status line, no redirects, no body. Uses
    the stdlib socket/ssl directly so the connected address cannot be re-
    resolved (DNS-rebinding safe). No credentials are sent."""
    import ssl
    raw = socket.create_connection((addr, port), timeout=PROBE_TIMEOUT_S)
    try:
        sock = raw
        if scheme == "https":
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(raw, server_hostname=host)
        req = (f"HEAD {path} HTTP/1.1\r\nHost: {host}\r\n"
               f"User-Agent: guild-reachability-probe/1\r\n"
               f"Accept: */*\r\nConnection: close\r\n\r\n")
        sock.sendall(req.encode("ascii", "ignore"))
        sock.settimeout(PROBE_TIMEOUT_S)
        buf = b""
        while b"\r\n" not in buf and len(buf) < PROBE_MAX_BYTES:
            chunk = sock.recv(min(512, PROBE_MAX_BYTES - len(buf)))
            if not chunk:
                break
            buf += chunk
        first = buf.split(b"\r\n", 1)[0].decode("ascii", "ignore")
        # "HTTP/1.1 200 OK" -> 200
        bits = first.split(" ")
        if len(bits) >= 2 and bits[0].startswith("HTTP/"):
            try:
                return int(bits[1])
            except ValueError:
                return None
        return None
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _rec(status: str, method: str, detail: str = "") -> dict[str, Any]:
    now = _now_iso()
    return {
        "status": status,
        "method": method,
        "checked_at": now,
        "last_verified_at": now if status in ("recently_reachable",
                                              "invocation_verified") else None,
        "detail": detail,
    }


def invocation_verified_record() -> dict[str, Any]:
    """Record for a guild-observed receipt that travelled through the endpoint —
    the only path to invocation_verified. Set by the store's receipt handler,
    never by a generic HTTP probe."""
    return _rec("invocation_verified", "guild_observed_receipt",
                detail="guild-observed task receipt through declared endpoint")


# --- 3. EFFECTIVE STATUS with expiry (pure) ----------------------------------

def _age_seconds(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


def status_for(endpoint: Optional[str], record: Optional[dict] = None) -> str:
    """Effective reachability status, applying TTL expiry to any stored
    verification record. Pure — no network."""
    if not endpoint:
        return "no_endpoint"
    ok, _ = url_policy_check(str(endpoint))
    if not ok:
        return "unknown"
    if record:
        st = record.get("status")
        age = _age_seconds(record.get("checked_at"))
        if st == "invocation_verified" and age is not None and age <= INVOCATION_TTL:
            return "invocation_verified"
        if st == "recently_reachable" and age is not None and age <= RECENT_TTL:
            return "recently_reachable"
        if st == "currently_unreachable" and age is not None and age <= UNREACH_TTL:
            return "currently_unreachable"
        # expired -> fall through to the plain declaration
    return "declared_unverified"


def reachability_fields(endpoint: Optional[str],
                        record: Optional[dict] = None) -> dict[str, Any]:
    """The full honest field set for one provider, honouring a stored
    verification record (with TTL expiry). Pure; callable from any read path
    without side effects."""
    status = status_for(endpoint, record)
    declared = status in ("declared_unverified", "recently_reachable",
                           "currently_unreachable", "invocation_verified")
    verified = status in ("recently_reachable", "invocation_verified")
    method = None
    last_verified_at = None
    age = None
    if status == "declared_unverified":
        method = "declaration_only"
    elif record and status in VERIFIED_STATUSES:
        method = record.get("method")
        last_verified_at = record.get("last_verified_at")
        age = _age_seconds(record.get("checked_at"))
        age = int(age) if age is not None else None
    return {
        "has_declared_endpoint": declared,
        "reachability_status": status,
        "verification_method": method,
        "last_verified_at": last_verified_at,
        "verification_age_seconds": age,
        # invocation_verified is the ONLY status that proves work went through
        "invocation_supported": status == "invocation_verified",
        "recommended_for_routing": status in ROUTABLE_STATUSES,
    }
