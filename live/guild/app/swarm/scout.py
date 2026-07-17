"""B3 — the demand-driven discovery scout.

Machine scouts find candidate SUPPLIERS for observed unmet demand — never
for guesses. The scout:

  * takes its work list ONLY from store.demand_feed_entries() (real, genuine,
    unmet demand — the same feed suppliers pull);
  * queries bounded registry adapters (official MCP Registry, the A2A
    registry's live JSON API, the x402 Bazaar facilitator catalogue;
    ERC-8004/Base is declared honestly unsupported until an indexer exists);
  * qualifies each candidate with the EVIDENCE its protocol actually offers:
      - A2A: the public agent card, fetched over an SSRF-safe pinned fetch;
      - MCP: the registry manifest IS the discovery evidence; reachability is
        a bounded Streamable HTTP `initialize` probe (an MCP endpoint answers
        POSTed JSON-RPC — it is never GETted expecting a JSON card);
      - x402: the Bazaar item IS the manifest; a valid HTTP 402 payment
        challenge IS protocol reachability (an unpaid priced resource is
        never required to return 200 JSON);
  * classifies every candidate `discovered_unverified` — discovery NEVER
    awards reputation, a hire verdict, or evidence. A candidate leaves that
    state only by registering and cryptographically participating (prove /
    signed receipts) like any other agent;
  * emits `candidate_discovered` ONLY on first sight and
    `candidate_refreshed` on every subsequent sighting;
  * never invokes or hires a candidate.

Every fetch/probe is SSRF-safe (policy check + DNS screening + pinned
connect, NO redirects, hard byte caps) with CORRECT bounded HTTP handling
(Content-Length and chunked transfer decoding — not line stripping).

Outbound machine contact is OFF by default (GUILD_SCOUT_CONTACT=1 enables
it) and even then happens only when the candidate's own card declares a
machine-contact endpoint AND terms that permit unsolicited machine contact,
with a disclosed Agent Guild identity, at most ONE candidate per capability
per 24 hours, an honoured opt-out list, and never a repeat to the same
candidate. `contact_attempted` and `contact_delivered` are recorded
separately: a failed send is never described as delivered. Otherwise the
demand feed is the outreach: suppliers pull it.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlparse, quote


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

from .. import reachability

# hard bounds — every adapter and fetch obeys them
MAX_CANDIDATES_PER_REGISTRY = 5
MAX_CAPABILITIES_PER_RUN = 10
MAX_CARD_BYTES = 128 * 1024
MAX_REGISTRY_BYTES = 2 * 1024 * 1024   # registry LIST endpoints are larger
MAX_HEADER_BYTES = 16 * 1024
FETCH_READ_TIMEOUT_S = 20.0            # bounded read (connect stays 3s;
                                       # the official MCP Registry routinely
                                       # takes ~10s to first byte)
MAX_STRING = 300                      # cap every string lifted from a card
CONTACT_MIN_INTERVAL_S = 86400.0      # 1 candidate/capability/24h
SCOUT_STATE_KEY = "scout"

CANDIDATE_STATUS = "discovered_unverified"

A2A_REGISTRY_API = "https://a2aregistry.org/api/agents"
MCP_REGISTRY_SEARCH = "https://registry.modelcontextprotocol.io/v0.1/servers"

SCOUT_UA = ("agent-guild-scout/1 (+https://agent-guild-5d5r."
            "onrender.com/.well-known/agent-guild.json)")


# ---------------------------------------------------------------------------
# SSRF-safe bounded HTTP (policy check → DNS screen → pinned connect,
# no redirects, hard byte caps, CORRECT header/Content-Length/chunked parse)
# ---------------------------------------------------------------------------

def _response_complete(buf: bytes) -> bool:
    """True when `buf` already holds one complete HTTP/1.1 response — so a
    server that ignores `Connection: close` cannot stall the read until the
    timeout."""
    head, sep, rest = buf.partition(b"\r\n\r\n")
    if not sep:
        return False
    headers = head.decode("iso-8859-1", "replace").lower()
    if "transfer-encoding" in headers and "chunked" in headers:
        return b"0\r\n\r\n" in rest or rest.endswith(b"0\r\n\r\n")
    for ln in headers.split("\r\n")[1:]:
        k, _, v = ln.partition(":")
        if k.strip() == "content-length" and v.strip().isdigit():
            return len(rest) >= int(v.strip())
    return False        # no framing: connection-close delimited → read to EOF


def _recv_bounded(sock, limit: int) -> bytes:
    buf = b""
    while len(buf) <= limit:
        try:
            chunk = sock.recv(min(8192, limit + 1 - len(buf)))
        except (TimeoutError, OSError):
            # a stalled server after a complete response is not a failure
            if _response_complete(buf):
                return buf
            raise
        if not chunk:
            break
        buf += chunk
        if _response_complete(buf):
            break
    return buf


def _decode_chunked(body: bytes, limit: int) -> Optional[bytes]:
    """Decode an HTTP/1.1 chunked transfer body, bounded. Returns None on a
    framing error or when the decoded size exceeds `limit`."""
    out = b""
    i = 0
    while True:
        j = body.find(b"\r\n", i)
        if j < 0:
            return None
        size_line = body[i:j].split(b";", 1)[0].strip()
        try:
            size = int(size_line, 16)
        except ValueError:
            return None
        if size == 0:
            return out
        start, end = j + 2, j + 2 + size
        if end > len(body) or len(out) + size > limit:
            return None
        out += body[start:end]
        if body[end:end + 2] != b"\r\n":
            return None
        i = end + 2


def parse_http_response(raw: bytes, max_body: int,
                        ) -> tuple[int, dict[str, str], Optional[bytes], str]:
    """Parse one buffered HTTP/1.1 response. Returns
    (status, headers, body|None, reason). Handles Content-Length and chunked
    transfer coding correctly, both bounded."""
    head, sep, rest = raw.partition(b"\r\n\r\n")
    if not sep:
        return 0, {}, None, "malformed_response"
    if len(head) > MAX_HEADER_BYTES:
        return 0, {}, None, "oversized_headers"
    lines = head.decode("iso-8859-1").split("\r\n")
    bits = lines[0].split(" ")
    if len(bits) < 2 or not bits[1].isdigit():
        return 0, {}, None, "malformed_status_line"
    status = int(bits[1])
    headers: dict[str, str] = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        if _:
            headers[k.strip().lower()] = v.strip()
    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = _decode_chunked(rest, max_body)
        if body is None:
            return status, headers, None, "oversized_or_malformed_chunked"
        return status, headers, body, "ok"
    cl = headers.get("content-length")
    if cl is not None and cl.isdigit():
        n = int(cl)
        if n > max_body:
            return status, headers, None, "oversized_response"
        return status, headers, rest[:n], "ok"
    # no framing header: connection-close delimited (we sent Connection:
    # close) — the buffered bytes are the body, subject to the cap.
    if len(rest) > max_body:
        return status, headers, None, "oversized_response"
    return status, headers, rest, "ok"


def safe_http_request(url: str, method: str = "GET",
                      body: Optional[bytes] = None,
                      headers: Optional[dict[str, str]] = None,
                      max_bytes: int = MAX_CARD_BYTES,
                      ) -> tuple[int, dict[str, str], bytes, str]:
    """One bounded HTTP request to a PUBLIC host, safely:
      * reachability.url_policy_check — scheme/port/credential rules;
      * every resolved A/AAAA address screened against loopback/private/
        link-local/reserved ranges (DNS-rebinding safe: the connection is
        PINNED to the screened address);
      * redirects are NOT followed (a 3xx is a failure — a redirect is how a
        hostile card walks a fetcher into an internal network);
      * headers and body reads are hard-capped.
    Returns (status, headers, body, reason); status 0 with a reason on any
    refusal/failure."""
    ok, reason = reachability.url_policy_check(url)
    if not ok:
        return 0, {}, b"", f"url_policy: {reason}"
    parts = urlparse(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    ok, addrs, reason = reachability._resolve_and_screen(host, port)
    if not ok:
        return 0, {}, b"", f"dns_screen: {reason}"
    family, addr = addrs[0]
    path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    hdrs = {"Host": host, "User-Agent": SCOUT_UA,
            "Accept": "application/json", "Connection": "close"}
    hdrs.update(headers or {})
    if body is not None:
        hdrs.setdefault("Content-Type", "application/json")
        hdrs["Content-Length"] = str(len(body))
    sock = None
    try:
        sock = reachability._connect_pinned(parts.scheme, host, family,
                                            addr, port)
        req = f"{method.upper()} {path} HTTP/1.1\r\n"
        req += "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())
        req += "\r\n"
        sock.sendall(req.encode("ascii", "ignore") + (body or b""))
        sock.settimeout(FETCH_READ_TIMEOUT_S)
        raw = _recv_bounded(sock, max_bytes + MAX_HEADER_BYTES)
        status, rhdrs, rbody, reason = parse_http_response(raw, max_bytes)
        if reason != "ok":
            return status, rhdrs, b"", reason
        if 300 <= status < 400:
            return status, rhdrs, b"", "redirect_refused"
        return status, rhdrs, rbody if rbody is not None else b"", "ok"
    except Exception as e:  # noqa: BLE001
        return 0, {}, b"", f"fetch_failed: {type(e).__name__}"
    finally:
        try:
            if sock:
                sock.close()
        except Exception:
            pass


def safe_fetch_json(url: str, max_bytes: int = MAX_CARD_BYTES,
                    ) -> tuple[Optional[Any], str]:
    """Fetch + parse one JSON document (GET, 200 only). Returns
    (parsed_json | None, reason)."""
    status, _hdrs, body, reason = safe_http_request(url, max_bytes=max_bytes)
    if reason != "ok":
        return None, reason
    if status != 200:
        return None, f"http_{status}"
    try:
        return json.loads(body.decode("utf-8", "replace")), "ok"
    except ValueError:
        return None, "invalid_json"


def _s(v: Any) -> str:
    """Hostile-card string guard: coerce + cap."""
    return str(v)[:MAX_STRING] if v is not None else ""


def canonical_capability(capability: str) -> str:
    """Shared canonical capability form (same rule the demand recorder
    uses), so the wake queue, demand rows and adapters all key alike."""
    from .. import demand as _demand
    return _demand.canonical_capability(capability)


# ---------------------------------------------------------------------------
# protocol probes (bounded, read-only, never paid)
# ---------------------------------------------------------------------------

def mcp_initialize_probe(endpoint: str,
                         request: Callable = safe_http_request,
                         ) -> dict[str, Any]:
    """Bounded Streamable HTTP MCP probe: POST a JSON-RPC `initialize` and
    accept a JSON or SSE answer. A parsed initialize result verifies the
    protocol; an auth-gated answer (401/403) proves something is listening
    without verifying it. Never GETs the endpoint expecting a JSON card."""
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18",
                   "capabilities": {},
                   "clientInfo": {"name": "agent-guild-scout",
                                  "version": "1"}},
    }).encode("utf-8")
    status, hdrs, body, reason = request(
        endpoint, method="POST", body=payload,
        headers={"Accept": "application/json, text/event-stream",
                 "MCP-Protocol-Version": "2025-06-18"})
    if reason != "ok":
        return {"reachable": False, "protocol_verified": False,
                "detail": reason}
    if status in (401, 403):
        return {"reachable": True, "protocol_verified": False,
                "detail": f"auth_required_http_{status}"}
    if status != 200:
        return {"reachable": False, "protocol_verified": False,
                "detail": f"http_{status}"}
    text = body.decode("utf-8", "replace")
    doc: Optional[dict] = None
    if "text/event-stream" in hdrs.get("content-type", ""):
        for ln in text.splitlines():                 # first SSE data event
            if ln.startswith("data:"):
                try:
                    doc = json.loads(ln[5:].strip())
                except ValueError:
                    doc = None
                break
    else:
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
    if (isinstance(doc, dict) and doc.get("jsonrpc") == "2.0"
            and isinstance(doc.get("result"), dict)):
        res = doc["result"]
        return {"reachable": True, "protocol_verified": True,
                "detail": "initialize_ok",
                "server_info": {k: _s(v) for k, v in
                                (res.get("serverInfo") or {}).items()},
                "protocol_version": _s(res.get("protocolVersion"))}
    return {"reachable": True, "protocol_verified": False,
            "detail": "http_200_but_no_initialize_result"}


def x402_challenge_probe(resource: str,
                         manifest: Optional[dict[str, Any]] = None,
                         request: Callable = safe_http_request,
                         ) -> dict[str, Any]:
    """Probe one x402 priced resource: a VALID HTTP 402 payment challenge
    (parseable x402 `accepts`, or a PAYMENT-REQUIRED header) IS protocol
    reachability. An unpaid resource is never required to return 200 JSON,
    and nothing is ever paid."""
    method = "GET"
    info = ((manifest or {}).get("extensions") or {}).get("bazaar") or {}
    declared = ((info.get("info") or {}).get("input") or {}).get("method")
    if isinstance(declared, str) and declared.upper() in ("GET", "POST"):
        method = declared.upper()
    body = b"{}" if method == "POST" else None
    status, hdrs, rbody, reason = request(resource, method=method, body=body)
    if reason != "ok":
        return {"reachable": False, "protocol_verified": False,
                "detail": reason}
    if status == 402:
        challenge_ok = bool(hdrs.get("payment-required"))
        if not challenge_ok:
            try:
                doc = json.loads(rbody.decode("utf-8", "replace"))
                challenge_ok = isinstance(doc, dict) and (
                    isinstance(doc.get("accepts"), list)
                    or "x402Version" in doc
                    or isinstance((doc.get("detail") or {}), dict)
                    and isinstance(doc["detail"].get("accepts"), list))
            except ValueError:
                challenge_ok = False
        return {"reachable": True, "protocol_verified": challenge_ok,
                "detail": ("http_402_challenge" if challenge_ok
                           else "http_402_without_parseable_challenge")}
    if 200 <= status < 500:
        # the resource answered, but did not present a payment challenge —
        # reachable as an HTTP endpoint, x402 protocol NOT verified.
        return {"reachable": True, "protocol_verified": False,
                "detail": f"http_{status}_no_402_challenge"}
    return {"reachable": False, "protocol_verified": False,
            "detail": f"http_{status}"}


# ---------------------------------------------------------------------------
# registry adapters (bounded, read-only)
# ---------------------------------------------------------------------------

def adapter_mcp_registry(capability: str, fetch: Callable) -> list[dict]:
    """Official MCP Registry search (registry.modelcontextprotocol.io). The
    returned manifest row travels with the candidate as its discovery
    evidence."""
    url = (f"{MCP_REGISTRY_SEARCH}"
           f"?search={quote(capability)}&limit={MAX_CANDIDATES_PER_REGISTRY}")
    doc, reason = fetch(url, max_bytes=MAX_REGISTRY_BYTES)
    out = []
    for row in (doc or {}).get("servers", [])[:MAX_CANDIDATES_PER_REGISTRY]:
        srv = row.get("server") if isinstance(row.get("server"), dict) else row
        if not isinstance(srv, dict):
            continue
        remotes = [r for r in (srv.get("remotes") or [])
                   if isinstance(r, dict)]
        endpoint = _s(remotes[0].get("url")) if remotes else ""
        out.append({"source": "mcp_registry", "source_url": url,
                    "name": _s(srv.get("name")),
                    "description": _s(srv.get("description")),
                    "endpoint": endpoint, "protocol": "mcp",
                    "manifest": srv,
                    "website": _s(srv.get("websiteUrl"))})
    return out


def adapter_a2a_registry(capability: str, fetch: Callable) -> list[dict]:
    """A2A registry — the LIVE JSON API at a2aregistry.org/api/agents
    (shape: {"agents": [...]}; /registry.json is an HTML SPA shell, not
    data). Candidates whose declared skills/description mention the
    capability."""
    url = A2A_REGISTRY_API
    doc, reason = fetch(url, max_bytes=MAX_REGISTRY_BYTES)
    if isinstance(doc, dict):
        agents = doc.get("agents") or []
    elif isinstance(doc, list):                # fixtures / legacy mirrors
        agents = doc
    else:
        agents = []
    needle = capability.lower()
    out = []
    for a in agents:
        if not isinstance(a, dict):
            continue
        blob = json.dumps(a).lower()
        if needle not in blob:
            continue
        endpoint = _s(a.get("url") or a.get("endpoint"))
        # health/validation fields the REGISTRY asserts about this agent:
        # carried as registry-attested evidence, clearly labelled — never
        # promoted into AG-independent verification.
        attested = {k: _s(a.get(k)) for k in
                    ("health", "status", "conformance", "lastChecked",
                     "lastValidated", "validated", "registryHealth")
                    if a.get(k) is not None}
        out.append({"source": "a2a_registry", "source_url": url,
                    "name": _s(a.get("name")),
                    "description": _s(a.get("description")),
                    "endpoint": endpoint, "protocol": "a2a",
                    "registry_attested": attested,
                    "card_url": _s(a.get("wellKnownURI")) or (
                        endpoint.rstrip("/") +
                        "/.well-known/agent-card.json" if endpoint else "")})
        if len(out) >= MAX_CANDIDATES_PER_REGISTRY:
            break
    return out


BAZAAR_PAGE_SIZE = 100
BAZAAR_PAGES_PER_CYCLE = 3   # bounded catalogue pages swept per cycle
# The canonical public Bazaar catalogue (CDP facilitator). Confirmed against
# primary docs (docs.cdp.coinbase.com/x402/bazaar, 2026-07-15):
#   * /discovery/search  — documented server-side semantic search (query +
#     filters, limit ≤ 20, response {"resources": [...]}); used FIRST;
#   * /discovery/resources — paginated catalogue (limit/offset, 25k+ items,
#     "browse order newest first"); a PERSISTENT cursor sweeps it to
#     completion a bounded number of pages per cycle (never restart at 0).
# DISCOVERY is read-only public data, independent of whichever facilitator
# settles OUR payments (the testnet facilitator serves no catalogue).
BAZAAR_DISCOVERY_BASE = "https://api.cdp.coinbase.com/platform/v2/x402"


def _bazaar_base() -> str:
    return (os.environ.get("GUILD_SCOUT_BAZAAR_URL")
            or BAZAAR_DISCOVERY_BASE).rstrip("/")


def canonical_sweep_key(capability: str) -> str:
    return canonical_capability(capability) or "_all"


def _sweep_state(store: Any, cap: str) -> dict[str, Any]:
    st = store.swarm_state.setdefault(SCOUT_STATE_KEY, {})
    sweeps = st.setdefault("bazaar_sweeps", {})
    return sweeps.setdefault(canonical_sweep_key(cap), {
        "cursor": 0, "pages_scanned": 0, "catalogue_total": 0,
        "candidates_found": 0, "last_complete_sweep_at": None,
        "coverage": 0.0})


def bazaar_sweep_stats(store: Any, cap: str) -> dict[str, Any]:
    return dict(_sweep_state(store, cap))


def _match_bazaar_item(it: Any, needle: str, url: str,
                       out: list[dict], seen: set) -> None:
    if not isinstance(it, dict):
        return
    res = _s(it.get("resource") or (it.get("resourceInfo") or {}).get("url"))
    if not res or res in seen:                       # dedupe by resource
        return
    if needle and needle not in json.dumps(it).lower():
        return
    seen.add(res)
    accepts = [a for a in (it.get("accepts") or []) if isinstance(a, dict)]
    out.append({"source": "x402_bazaar", "source_url": url,
                "name": _s(it.get("serviceName") or it.get("name") or res),
                "description": _s(it.get("description")),
                "endpoint": res, "protocol": "x402", "manifest": it,
                "wallet": _s(it.get("payTo") or (
                    accepts[0].get("payTo") if accepts else ""))})


def adapter_x402_bazaar(capability: str, fetch: Callable,
                        store: Any = None) -> list[dict]:
    """x402 Bazaar discovery, eventually complete.

    1. DOCUMENTED SERVER-SIDE SEARCH first (/discovery/search) — relevance +
       filters, {"resources": [...]}; the fast path for a specific
       capability.
    2. PERSISTENT-CURSOR CATALOGUE SWEEP (/discovery/resources) — a bounded
       number of pages per cycle, RESUMING from the per-capability cursor so
       the 25k+-item catalogue is swept to completion across cycles instead
       of re-scanning page 0 forever. Duplicates (by resource) collapse; an
       out-of-range cursor (catalogue shrank) resets; coverage/cursor/pages/
       last-complete-sweep are recorded for /swarm/status."""
    if store is None:
        from ..state import store as _store
        store = _store
    base = _bazaar_base()
    needle = (capability or "").lower()
    out: list[dict] = []
    seen: set = set()

    # 1. documented semantic search (limit hard-capped at 20 by CDP). CDP
    # already ranked these by relevance server-side, so they are accepted
    # without re-applying the literal-substring needle.
    surl = f"{base}/discovery/search?query={quote(capability)}&limit=20"
    sdoc, _sr = fetch(surl, max_bytes=MAX_REGISTRY_BYTES)
    for it in (sdoc or {}).get("resources", []):
        _match_bazaar_item(it, "", surl, out, seen)
        if len(out) >= MAX_CANDIDATES_PER_REGISTRY:
            return out

    # 2. persistent-cursor catalogue sweep
    sw = _sweep_state(store, capability)
    total = int(sw.get("catalogue_total") or 0)
    cursor = int(sw.get("cursor") or 0)
    if total and cursor >= total:            # sweep complete OR shrank: reset
        cursor = 0
    pages = 0
    while pages < BAZAAR_PAGES_PER_CYCLE:
        url = (f"{base}/discovery/resources"
               f"?limit={BAZAAR_PAGE_SIZE}&offset={cursor}")
        doc, _r = fetch(url, max_bytes=MAX_REGISTRY_BYTES)
        items = (doc or {}).get("items") or (doc or {}).get("resources") or []
        total = int(((doc or {}).get("pagination") or {}).get("total")
                    or total)
        if cursor > max(total, 0):           # expired/out-of-range cursor
            cursor = 0
            continue
        pages += 1
        sw["pages_scanned"] = int(sw.get("pages_scanned") or 0) + 1
        for it in items:
            _match_bazaar_item(it, needle, url, out, seen)
        cursor += BAZAAR_PAGE_SIZE
        if not items or len(items) < BAZAAR_PAGE_SIZE or (
                total and cursor >= total):
            # reached the end of the catalogue → sweep complete, restart next
            sw["last_complete_sweep_at"] = _now_iso()
            cursor = 0
            break
        if len(out) >= MAX_CANDIDATES_PER_REGISTRY:
            break
    sw["cursor"] = cursor
    sw["catalogue_total"] = total
    sw["candidates_found"] = int(sw.get("candidates_found") or 0) + len(out)
    sw["coverage"] = round(min(1.0, (sw["pages_scanned"] * BAZAAR_PAGE_SIZE)
                               / total), 4) if total else 0.0
    return out[:MAX_CANDIDATES_PER_REGISTRY]


def adapter_erc8004(capability: str, fetch: Callable) -> list[dict]:
    """ERC-8004 / Base on-chain agent discovery. HONESTY: reading the
    identity registry requires an indexer or bounded log scans this scout
    does not ship yet — returning [] with a declared reason instead of
    pretending coverage."""
    return []


ADAPTERS: dict[str, Callable[[str, Callable], list[dict]]] = {
    "mcp_registry": adapter_mcp_registry,
    "a2a_registry": adapter_a2a_registry,
    "x402_bazaar": adapter_x402_bazaar,
    "erc8004": adapter_erc8004,
}


# ---------------------------------------------------------------------------
# candidate qualification (never awards trust)
# ---------------------------------------------------------------------------

def _validate_card(card: Any, protocol: str) -> tuple[bool, dict[str, Any]]:
    """Validate + sanitise discovery evidence (an A2A card, an MCP registry
    manifest, or an x402 Bazaar item). Hostile inputs are strings of
    unbounded length, wrong shapes, or scripts — everything lifted is
    type-checked and capped."""
    if not isinstance(card, dict):
        return False, {}
    facts: dict[str, Any] = {}
    if protocol == "a2a":
        if not card.get("name") or not (card.get("url") or card.get("skills")):
            return False, {}
        facts["card_name"] = _s(card.get("name"))
        facts["card_url_field"] = _s(card.get("url"))
        facts["skills"] = [_s((sk or {}).get("id"))
                           for sk in (card.get("skills") or [])[:20]
                           if isinstance(sk, dict)]
        facts["provider"] = _s((card.get("provider") or {}).get(
            "organization") if isinstance(card.get("provider"), dict) else "")
    elif protocol == "mcp":
        # the official registry manifest: server name + streamable-http remote
        if not card.get("name"):
            return False, {}
        facts["card_name"] = _s(card.get("name"))
        remotes = [r for r in (card.get("remotes") or [])
                   if isinstance(r, dict)]
        if remotes:
            facts["remote_type"] = _s(remotes[0].get("type"))
            facts["remote_url"] = _s(remotes[0].get("url"))
    elif protocol == "x402":
        # the Bazaar item: a priced resource with x402 payment requirements
        if not card.get("resource") or not isinstance(card.get("accepts"),
                                                      list):
            return False, {}
        facts["card_name"] = _s(card.get("serviceName") or card.get("name")
                                or card.get("resource"))
        facts["resource"] = _s(card.get("resource"))
        acc = [a for a in card["accepts"] if isinstance(a, dict)]
        if acc:
            facts["network"] = _s(acc[0].get("network"))
            facts["declared_wallet"] = _s(acc[0].get("payTo"))
    else:
        facts["card_name"] = _s(card.get("name"))
    # identity/domain/wallet bindings, RECORDED where present — never trusted
    for k_src, k_dst in (("did", "declared_did"),
                         ("issuer_did", "declared_did"),
                         ("payTo", "declared_wallet"),
                         ("wallet", "declared_wallet")):
        if card.get(k_src):
            facts[k_dst] = _s(card.get(k_src))
    return True, facts


def qualify_candidate(cand: dict, fetch: Callable,
                      probe: Optional[Callable] = None,
                      mcp_probe: Optional[Callable] = None,
                      x402_probe: Optional[Callable] = None) -> dict:
    """Validate the candidate's discovery evidence and record it in FIVE
    strictly separated classes (rec["evidence"]) that are never conflated:

      discovery_document_reachable  AG fetched/holds the candidate's own
                                    discovery document (A2A agent card / MCP
                                    registry manifest / x402 Bazaar item);
      discovery_protocol_verified   that discovery document is VALID for the
                                    declared protocol;
      execution_endpoint_reachable  the candidate's EXECUTION endpoint itself
                                    answered AG (only via a genuinely
                                    side-effect-free protocol operation);
      execution_protocol_verified   the execution endpoint answered with its
                                    DECLARED protocol via a side-effect-free
                                    operation;
      registry_attested             health/conformance the REGISTRY asserts —
                                    recorded verbatim, clearly labelled,
                                    NEVER promoted into AG-independent
                                    evidence.

    Per protocol — never a paid call, never work creation, never a
    generic-GET of an execution endpoint:
      * a2a  — the public agent card (a side-effect-free discovery step)
               proves ONLY discovery_document_reachable +
               discovery_protocol_verified. The A2A spec offers no
               side-effect-free EXECUTION operation (a message-send creates
               work), so execution_* stay FALSE — a card is never execution
               proof, and no message is ever sent to probe;
      * mcp  — the registry manifest is the discovery document; the bounded
               Streamable HTTP `initialize` probe is a genuinely
               side-effect-free EXECUTION operation → it sets execution_*;
      * x402 — the Bazaar item is the discovery document; a valid unpaid
               HTTP 402 challenge is a side-effect-free EXECUTION probe →
               it sets execution_*.

    Only AG-INDEPENDENT execution evidence emits candidate_execution_verified
    and the legacy candidate_endpoint_verified; a valid discovery document
    emits candidate_discovery_verified. Returns the candidate record —
    ALWAYS `discovered_unverified`: being discoverable is not evidence."""
    mcp_probe = mcp_probe or mcp_initialize_probe
    x402_probe = x402_probe or x402_challenge_probe
    protocol = cand.get("protocol", "")
    now = time.time()
    rec = {**cand, "status": CANDIDATE_STATUS,
           "first_seen": cand.get("first_seen") or now,
           "last_seen": now,
           "card_valid": False, "endpoint_reachable": False,
           "protocol_declared": protocol,
           "bindings": {}, "checks": {}}
    evidence: dict[str, Any] = {
        "discovery_document_reachable": False,
        "discovery_protocol_verified": False,
        "execution_endpoint_reachable": False,
        "execution_protocol_verified": False,
        "registry_attested": dict(cand.get("registry_attested") or {}),
        "registry_attested_note": (
            "attested by the registry, not verified by Agent Guild — "
            "never promoted into AG-independent evidence"),
        # kept for back-compat with the previous 4-class readers.
        "ag_verified": {"card_valid": False},
        "independently_reachable": False,
        "protocol_verified": False,
    }
    rec["evidence"] = evidence
    endpoint = cand.get("endpoint") or ""
    ok, reason = reachability.url_policy_check(endpoint) if endpoint else (
        False, "no endpoint")
    rec["checks"]["endpoint_policy"] = reason if not ok else "ok"
    if not ok:
        return rec

    # --- DISCOVERY-document evidence (protocol-appropriate; never paid) -----
    if protocol == "mcp":
        manifest = cand.get("manifest")
        valid, facts = _validate_card(manifest, protocol)
        rec["checks"]["evidence"] = "registry_manifest"
        evidence["discovery_document_reachable"] = manifest is not None
        evidence["discovery_protocol_verified"] = valid
        rec["card_valid"] = valid
        if valid:
            rec["card_facts"] = facts
            rec["bindings"] = {k: v for k, v in facts.items()
                               if k.startswith("declared_")}
    elif protocol == "x402":
        manifest = cand.get("manifest")
        valid, facts = _validate_card(manifest, protocol)
        rec["checks"]["evidence"] = "bazaar_item"
        evidence["discovery_document_reachable"] = manifest is not None
        evidence["discovery_protocol_verified"] = valid
        rec["card_valid"] = valid
        if valid:
            rec["card_facts"] = facts
            rec["bindings"] = {k: v for k, v in facts.items()
                               if k.startswith("declared_")}
    else:  # a2a: the public agent card (side-effect-free discovery step)
        card_url = cand.get("card_url") or (
            endpoint.rstrip("/") + "/.well-known/agent-card.json")
        card, fetch_reason = fetch(card_url)
        rec["checks"]["card_fetch"] = fetch_reason
        evidence["discovery_document_reachable"] = (fetch_reason == "ok"
                                                    and card is not None)
        if card is not None:
            valid, facts = _validate_card(card, protocol)
            evidence["discovery_protocol_verified"] = valid
            rec["card_valid"] = valid
            if valid:
                rec["bindings"] = {k: v for k, v in facts.items()
                                   if k.startswith("declared_")}
                rec["card_facts"] = facts
                cu = urlparse(facts.get("card_url_field") or "")
                eu = urlparse(endpoint)
                if cu.hostname and eu.hostname:
                    binding = ("match" if cu.hostname == eu.hostname
                               else "mismatch")
                    rec["checks"]["domain_binding"] = binding

    # --- EXECUTION-endpoint evidence (only side-effect-free operations) -----
    try:
        if protocol == "mcp":
            live = mcp_probe(endpoint)
            evidence["execution_endpoint_reachable"] = bool(
                live.get("reachable"))
            evidence["execution_protocol_verified"] = bool(
                live.get("protocol_verified"))
            rec["checks"]["protocol_probe"] = _s(live.get("detail"))
        elif protocol == "x402":
            live = x402_probe(endpoint, manifest=cand.get("manifest"))
            evidence["execution_endpoint_reachable"] = bool(
                live.get("reachable"))
            evidence["execution_protocol_verified"] = bool(
                live.get("protocol_verified"))
            rec["checks"]["protocol_probe"] = _s(live.get("detail"))
        elif protocol == "a2a":
            # A2A has NO side-effect-free execution operation (a message-send
            # creates work), so execution verification honestly stays FALSE.
            # The execution endpoint is never generic-GETted or sent a message.
            rec["checks"]["protocol_probe"] = (
                "a2a_execution_verification_unavailable: the A2A spec has no "
                "side-effect-free execution probe; card proves discovery only")
        else:
            live = (probe or reachability.liveness_probe)(endpoint)
            evidence["execution_endpoint_reachable"] = bool(
                live.get("reachable"))
            rec["checks"]["liveness"] = _s(live.get("detail")
                                           or live.get("status"))
    except Exception as e:  # noqa: BLE001
        rec["checks"]["liveness"] = f"probe_failed: {type(e).__name__}"

    # back-compat mirror for the previous 4-class readers: "protocol_verified"
    # / "independently_reachable" reflect INDEPENDENT execution evidence only
    # (never card-only), so callers that gated on them keep their meaning.
    evidence["ag_verified"]["card_valid"] = rec["card_valid"]
    evidence["independently_reachable"] = evidence[
        "execution_endpoint_reachable"]
    evidence["protocol_verified"] = evidence["execution_protocol_verified"]
    rec["checks"]["protocol_verified"] = evidence["execution_protocol_verified"]
    rec["endpoint_reachable"] = evidence["execution_endpoint_reachable"]
    return rec


# ---------------------------------------------------------------------------
# the run: observed unmet demand → bounded discovery → recorded candidates
# ---------------------------------------------------------------------------

def _scout_state(store: Any) -> dict[str, Any]:
    st = store.swarm_state.setdefault(SCOUT_STATE_KEY, {})
    st.setdefault("candidates", {})
    st.setdefault("contacts", {})          # capability -> last contact epoch
    st.setdefault("contacted_endpoints", {})
    st.setdefault("opt_out", [])
    return st


def _persist(store: Any) -> None:
    with store.lock, store._txn():
        if store.backend is not None:
            store._persist_kv("swarm_state", store.swarm_state)
        store._save()


def run_scout(store: Any, fetch: Callable = safe_fetch_json,
              probe: Optional[Callable] = None,
              adapters: Optional[dict[str, Callable]] = None,
              deadline: Optional[float] = None,
              ) -> dict[str, Any]:
    """One bounded scout pass: for each observed unmet-demand capability
    (genuine external asks only), query each registry adapter, qualify the
    candidates, and record them `discovered_unverified`. First sight emits
    `candidate_discovered`; every later sighting emits `candidate_refreshed`
    — a refresh never inflates discovery. Never registers, never invokes,
    never awards evidence, never contacts (see maybe_contact). An optional
    `deadline` (epoch seconds) bounds the whole pass."""
    adapters = adapters if adapters is not None else ADAPTERS
    demand_rows = [r for r in store.demand_feed_entries()
                   if r["genuine_lookups"] > 0][:MAX_CAPABILITIES_PER_RUN]
    st = _scout_state(store)
    # TRUTHFUL adapter status: an adapter that never ran must never read as
    # ok. Zero demand ⇒ every adapter is skipped with the reason stated.
    summary: dict[str, Any] = {
        "capabilities": [], "discovered": 0, "refreshed": 0,
        "endpoint_verified": 0, "adapters_failed": [],
        "adapters": {name: {"status": "not_run", "reason": "no_demand",
                            "candidates": 0}
                     for name in adapters},
        "deadline_hit": False,
    }
    for row in demand_rows:
        if deadline is not None and time.time() > deadline:
            summary["deadline_hit"] = True
            break
        cap = row["capability"]
        summary["capabilities"].append(cap)
        for name, adapter in adapters.items():
            if deadline is not None and time.time() > deadline:
                summary["deadline_hit"] = True
                break
            try:
                cands = adapter(cap, fetch)[:MAX_CANDIDATES_PER_REGISTRY]
            except Exception as e:  # noqa: BLE001
                summary["adapters_failed"].append(
                    {"adapter": name, "capability": cap,
                     "reason": f"{type(e).__name__}"})
                summary["adapters"][name].update(
                    status="failed", ok=False,
                    reason=f"{type(e).__name__}")
                continue
            a = summary["adapters"][name]
            if a["status"] != "failed":
                a.update(status="ran", ok=True)
                a.pop("reason", None)
            a["candidates"] += len(cands)
            for cand in cands:
                cand["capability"] = cap
                key = f"{cand.get('source')}:{cand.get('endpoint')}"
                prior = st["candidates"].get(key)
                first_sight = prior is None
                if prior:
                    cand["first_seen"] = prior.get("first_seen")
                rec = qualify_candidate(cand, fetch, probe)
                st["candidates"][key] = rec
                if first_sight:
                    summary["discovered"] += 1
                    store.record_event(None, "candidate_discovered",
                                       capability=cap,
                                       source=cand.get("source"),
                                       status=CANDIDATE_STATUS,
                                       endpoint_reachable=rec[
                                           "endpoint_reachable"],
                                       scout=True)
                else:
                    summary["refreshed"] += 1
                    store.record_event(None, "candidate_refreshed",
                                       capability=cap,
                                       source=cand.get("source"),
                                       status=CANDIDATE_STATUS,
                                       endpoint_reachable=rec[
                                           "endpoint_reachable"],
                                       scout=True)
                ev = rec.get("evidence") or {}
                # DISCOVERY-document verification: its own event.
                if ev.get("discovery_protocol_verified"):
                    store.record_event(None, "candidate_discovery_verified",
                                       capability=cap,
                                       source=cand.get("source"),
                                       scout=True)
                # AG-INDEPENDENT EXECUTION verification: a SEPARATE event, and
                # the ONLY thing that fires the legacy endpoint_verified —
                # never from card-only discovery evidence.
                if ev.get("execution_protocol_verified"):
                    summary["endpoint_verified"] += 1
                    summary["execution_verified"] = summary.get(
                        "execution_verified", 0) + 1
                    store.record_event(None, "candidate_execution_verified",
                                       capability=cap,
                                       source=cand.get("source"),
                                       scout=True)
                    store.record_event(None, "candidate_endpoint_verified",
                                       capability=cap,
                                       source=cand.get("source"),
                                       scout=True)
    _persist(store)
    return summary


# ---------------------------------------------------------------------------
# outbound contact — OFF by default; terms-gated, rate-limited, opt-out
# ---------------------------------------------------------------------------

def contact_enabled() -> bool:
    return (os.environ.get("GUILD_SCOUT_CONTACT") or "0").strip() == "1"


def card_permits_contact(card: dict[str, Any]) -> bool:
    """Outbound machine contact only when the candidate's PUBLIC card
    explicitly permits it: a declared machine-contact endpoint plus a terms
    field that allows unsolicited machine messages. Absence of terms means
    NO."""
    if not isinstance(card, dict):
        return False
    terms = card.get("contact_policy") or card.get("machine_contact") or {}
    if isinstance(terms, str):
        return terms.lower() in ("open", "machine-contact-welcome",
                                 "unsolicited-ok")
    if isinstance(terms, dict):
        return bool(terms.get("unsolicited") in (True, "allowed", "ok")
                    and terms.get("endpoint"))
    return False


def record_opt_out(store: Any, endpoint: str) -> None:
    st = _scout_state(store)
    if endpoint not in st["opt_out"]:
        st["opt_out"].append(endpoint)
        _persist(store)


def maybe_contact(store: Any, candidate: dict[str, Any],
                  card: dict[str, Any],
                  send: Callable[[str, dict[str, Any]], Any],
                  now: Optional[float] = None) -> dict[str, Any]:
    """Contact ONE candidate about observed demand — only if every gate
    passes:
      1. GUILD_SCOUT_CONTACT=1 (default OFF: the demand feed is the outreach);
      2. the candidate's own card permits unsolicited machine contact;
      3. the endpoint has not opted out;
      4. at most one candidate per capability per 24h;
      5. never the same endpoint twice.
    The message DISCLOSES the Agent Guild identity and carries an automatic
    opt-out action. Events: `contact_attempted` is recorded before the send;
    `contact_delivered` ONLY after a successful send — a failed send is
    never described as delivered."""
    now = time.time() if now is None else now
    st = _scout_state(store)
    cap = candidate.get("capability") or ""
    endpoint = candidate.get("endpoint") or ""
    if not contact_enabled():
        return {"contacted": False, "reason": "contact_disabled_default"}
    if not card_permits_contact(card):
        return {"contacted": False, "reason": "terms_do_not_permit"}
    if endpoint in st["opt_out"]:
        return {"contacted": False, "reason": "opted_out"}
    if endpoint in st["contacted_endpoints"]:
        return {"contacted": False, "reason": "already_contacted_once"}
    last = float(st["contacts"].get(cap) or 0.0)
    if now - last < CONTACT_MIN_INTERVAL_S:
        return {"contacted": False, "reason": "capability_rate_limited_24h"}
    message = {
        "from": {
            "name": "Agent Guild discovery scout",
            "identity_document": ("https://agent-guild-5d5r.onrender.com"
                                  "/.well-known/agent-guild.json"),
            "did_document": ("https://agent-guild-5d5r.onrender.com"
                             "/.well-known/did.json"),
        },
        "reason": (f"observed genuine unmet machine demand for "
                   f"'{cap}' (aggregate counts only)"),
        "demand_feed": ("https://agent-guild-5d5r.onrender.com/demand/feed"),
        "register": ("POST https://agent-guild-5d5r.onrender.com"
                     "/agents/register (free)"),
        "opt_out": {"how": "reply with {\"opt_out\": true} — honoured "
                           "automatically and permanently; you will never "
                           "be messaged again"},
        "one_time": True,
    }
    st["contacts"][cap] = now
    st["contacted_endpoints"][endpoint] = now
    _persist(store)
    store.record_event(None, "contact_attempted", capability=cap,
                       scout=True, disclosed_identity=True)
    try:
        send(endpoint, message)
    except Exception as e:  # noqa: BLE001
        return {"contacted": True, "delivered": False,
                "reason": f"send_failed: {type(e).__name__}"}
    store.record_event(None, "contact_delivered", capability=cap,
                       scout=True, disclosed_identity=True)
    return {"contacted": True, "delivered": True}
