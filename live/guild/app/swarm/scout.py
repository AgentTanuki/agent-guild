"""B3 — the demand-driven discovery scout.

Machine scouts find candidate SUPPLIERS for observed unmet demand — never
for guesses. The scout:

  * takes its work list ONLY from store.demand_feed_entries() (real, genuine,
    unmet demand — the same feed suppliers pull);
  * queries bounded registry adapters (official MCP Registry, A2A registry,
    the x402 Bazaar facilitator catalogue; ERC-8004/Base is declared honestly
    unsupported until an indexer exists);
  * for each candidate: fetches and validates its public card/manifest over
    an SSRF-safe pinned fetch (policy check + DNS screening + pinned connect,
    NO redirects, hard byte cap, string caps against hostile cards), verifies
    endpoint reachability and the declared protocol, verifies identity/
    domain/wallet bindings where the card carries them (recorded, never
    trusted), and records source + last-seen;
  * classifies every candidate `discovered_unverified` — discovery NEVER
    awards reputation, a hire verdict, or evidence. A candidate leaves that
    state only by registering and cryptographically participating (prove /
    signed receipts) like any other agent;
  * never invokes or hires a candidate.

Outbound machine contact is OFF by default (GUILD_SCOUT_CONTACT=1 enables
it) and even then happens only when the candidate's own card declares a
machine-contact endpoint AND terms that permit unsolicited machine contact,
with a disclosed Agent Guild identity, at most ONE candidate per capability
per 24 hours, an honoured opt-out list, and never a repeat to the same
candidate. Otherwise the demand feed is the outreach: suppliers pull it.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse, quote

from .. import reachability

# hard bounds — every adapter and fetch obeys them
MAX_CANDIDATES_PER_REGISTRY = 5
MAX_CAPABILITIES_PER_RUN = 10
MAX_CARD_BYTES = 128 * 1024
MAX_STRING = 300                      # cap every string lifted from a card
CONTACT_MIN_INTERVAL_S = 86400.0      # 1 candidate/capability/24h
SCOUT_STATE_KEY = "scout"

CANDIDATE_STATUS = "discovered_unverified"


# ---------------------------------------------------------------------------
# SSRF-safe bounded fetch (policy check → DNS screen → pinned connect,
# no redirects, hard byte cap)
# ---------------------------------------------------------------------------

def safe_fetch_json(url: str, max_bytes: int = MAX_CARD_BYTES,
                    ) -> tuple[Optional[Any], str]:
    """Fetch + parse one JSON document from a PUBLIC host, safely:
      * reachability.url_policy_check — scheme/port/credential rules;
      * every resolved A/AAAA address screened against loopback/private/
        link-local/reserved ranges (DNS-rebinding safe: the connection is
        PINNED to the screened address);
      * redirects are NOT followed (a 3xx is a failure — a redirect is how a
        hostile card walks a fetcher into an internal network);
      * the body read is hard-capped at `max_bytes`.
    Returns (parsed_json | None, reason)."""
    ok, reason = reachability.url_policy_check(url)
    if not ok:
        return None, f"url_policy: {reason}"
    parts = urlparse(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    ok, addrs, reason = reachability._resolve_and_screen(host, port)
    if not ok:
        return None, f"dns_screen: {reason}"
    family, addr = addrs[0]
    path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    sock = None
    try:
        sock = reachability._connect_pinned(parts.scheme, host, family,
                                            addr, port)
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
               f"User-Agent: agent-guild-scout/1 (+https://agent-guild-5d5r."
               f"onrender.com/.well-known/agent-guild.json)\r\n"
               f"Accept: application/json\r\nConnection: close\r\n\r\n")
        sock.sendall(req.encode("ascii", "ignore"))
        sock.settimeout(reachability.PROBE_TIMEOUT_S)
        buf = b""
        while len(buf) < max_bytes:
            chunk = sock.recv(min(4096, max_bytes - len(buf)))
            if not chunk:
                break
            buf += chunk
        if len(buf) >= max_bytes:
            return None, "oversized_response"
        head, _, body = buf.partition(b"\r\n\r\n")
        first = head.split(b"\r\n", 1)[0].decode("ascii", "ignore")
        bits = first.split(" ")
        code = int(bits[1]) if len(bits) >= 2 and bits[1].isdigit() else 0
        if 300 <= code < 400:
            return None, "redirect_refused"
        if code != 200:
            return None, f"http_{code}"
        # tolerate chunked transfer crudely: JSON parse decides
        text = body.decode("utf-8", "replace")
        try:
            return json.loads(text), "ok"
        except ValueError:
            # chunked bodies: strip chunk-size lines and retry once
            lines = [ln for ln in text.splitlines()
                     if not all(c in "0123456789abcdefABCDEF" for c in
                                ln.strip()) or not ln.strip()]
            try:
                return json.loads("\n".join(lines)), "ok"
            except ValueError:
                return None, "invalid_json"
    except Exception as e:  # noqa: BLE001
        return None, f"fetch_failed: {type(e).__name__}"
    finally:
        try:
            if sock:
                sock.close()
        except Exception:
            pass


def _s(v: Any) -> str:
    """Hostile-card string guard: coerce + cap."""
    return str(v)[:MAX_STRING] if v is not None else ""


# ---------------------------------------------------------------------------
# registry adapters (bounded, read-only)
# ---------------------------------------------------------------------------

def adapter_mcp_registry(capability: str, fetch: Callable) -> list[dict]:
    """Official MCP Registry search (registry.modelcontextprotocol.io)."""
    url = ("https://registry.modelcontextprotocol.io/v0.1/servers"
           f"?search={quote(capability)}&limit={MAX_CANDIDATES_PER_REGISTRY}")
    doc, reason = fetch(url)
    out = []
    for row in (doc or {}).get("servers", [])[:MAX_CANDIDATES_PER_REGISTRY]:
        srv = row.get("server") if isinstance(row.get("server"), dict) else row
        if not isinstance(srv, dict):
            continue
        remotes = srv.get("remotes") or []
        endpoint = _s(remotes[0].get("url")) if remotes else ""
        out.append({"source": "mcp_registry", "source_url": url,
                    "name": _s(srv.get("name")),
                    "description": _s(srv.get("description")),
                    "endpoint": endpoint, "protocol": "mcp",
                    "card_url": endpoint,
                    "website": _s(srv.get("websiteUrl"))})
    return out


def adapter_a2a_registry(capability: str, fetch: Callable) -> list[dict]:
    """A2A registry (a2aregistry.org public list) — candidates whose declared
    skills/description mention the capability."""
    url = "https://a2aregistry.org/registry.json"
    doc, reason = fetch(url)
    agents = doc if isinstance(doc, list) else (doc or {}).get("agents", [])
    needle = capability.lower()
    out = []
    for a in agents:
        if not isinstance(a, dict):
            continue
        blob = json.dumps(a).lower()
        if needle not in blob:
            continue
        endpoint = _s(a.get("url") or a.get("endpoint"))
        out.append({"source": "a2a_registry", "source_url": url,
                    "name": _s(a.get("name")),
                    "description": _s(a.get("description")),
                    "endpoint": endpoint, "protocol": "a2a",
                    "card_url": _s(a.get("wellKnownURI")) or (
                        endpoint.rstrip("/") +
                        "/.well-known/agent-card.json" if endpoint else "")})
        if len(out) >= MAX_CANDIDATES_PER_REGISTRY:
            break
    return out


def adapter_x402_bazaar(capability: str, fetch: Callable) -> list[dict]:
    """x402 Bazaar: the facilitator's machine-readable catalogue of priced
    resources (specs/extensions/bazaar.md discovery list)."""
    from .. import x402 as x402_mod
    base = x402_mod.facilitator_url()
    url = f"{base}/discovery/resources?limit={MAX_CANDIDATES_PER_REGISTRY}"
    doc, reason = fetch(url)
    items = (doc or {}).get("items") or (doc or {}).get("resources") or []
    needle = capability.lower()
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if needle not in json.dumps(it).lower():
            continue
        res = _s(it.get("resource") or (it.get("resourceInfo") or {}
                                        ).get("url"))
        out.append({"source": "x402_bazaar", "source_url": url,
                    "name": _s(it.get("name") or res),
                    "description": _s(it.get("description")),
                    "endpoint": res, "protocol": "x402",
                    "card_url": res,
                    "wallet": _s(it.get("payTo") or (it.get("accepts") or
                                 [{}])[0].get("payTo") if it else "")})
        if len(out) >= MAX_CANDIDATES_PER_REGISTRY:
            break
    return out


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
    """Validate + sanitise a fetched public card. Hostile cards are strings
    of unbounded length, wrong shapes, or scripts — everything lifted is
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
        facts["card_name"] = _s(card.get("name") or card.get("serverInfo"))
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
                      probe: Optional[Callable] = None) -> dict:
    """Fetch + validate the candidate's public card, verify endpoint
    reachability and declared protocol, verify declared bindings where
    available. Returns the candidate record — ALWAYS classified
    `discovered_unverified`: being discoverable is not evidence."""
    probe = probe or reachability.liveness_probe
    now = time.time()
    rec = {**cand, "status": CANDIDATE_STATUS,
           "first_seen": cand.get("first_seen") or now,
           "last_seen": now,
           "card_valid": False, "endpoint_reachable": False,
           "protocol_declared": cand.get("protocol", ""),
           "bindings": {}, "checks": {}}
    endpoint = cand.get("endpoint") or ""
    ok, reason = reachability.url_policy_check(endpoint) if endpoint else (
        False, "no endpoint")
    rec["checks"]["endpoint_policy"] = reason if not ok else "ok"
    if not ok:
        return rec
    card_url = cand.get("card_url") or endpoint
    card, fetch_reason = fetch(card_url)
    rec["checks"]["card_fetch"] = fetch_reason
    if card is not None:
        valid, facts = _validate_card(card, cand.get("protocol", ""))
        rec["card_valid"] = valid
        if valid:
            rec["bindings"] = {k: v for k, v in facts.items()
                               if k.startswith("declared_")}
            rec["card_facts"] = facts
            # domain binding: does the card's own url match the endpoint host?
            cu = urlparse(facts.get("card_url_field") or "")
            eu = urlparse(endpoint)
            if cu.hostname and eu.hostname:
                rec["checks"]["domain_binding"] = (
                    "match" if cu.hostname == eu.hostname else "mismatch")
    try:
        live = probe(endpoint)
        rec["endpoint_reachable"] = bool(live.get("reachable"))
        rec["checks"]["liveness"] = _s(live.get("detail")
                                       or live.get("status"))
    except Exception as e:  # noqa: BLE001
        rec["checks"]["liveness"] = f"probe_failed: {type(e).__name__}"
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
              ) -> dict[str, Any]:
    """One bounded scout pass: for each observed unmet-demand capability
    (genuine external asks only), query each registry adapter, qualify the
    candidates, and record them `discovered_unverified`. Never registers,
    never invokes, never awards evidence, never contacts (see
    maybe_contact)."""
    adapters = adapters if adapters is not None else ADAPTERS
    demand_rows = [r for r in store.demand_feed_entries()
                   if r["genuine_lookups"] > 0][:MAX_CAPABILITIES_PER_RUN]
    st = _scout_state(store)
    summary = {"capabilities": [], "discovered": 0, "adapters_failed": []}
    for row in demand_rows:
        cap = row["capability"]
        summary["capabilities"].append(cap)
        for name, adapter in adapters.items():
            try:
                cands = adapter(cap, fetch)[:MAX_CANDIDATES_PER_REGISTRY]
            except Exception as e:  # noqa: BLE001
                summary["adapters_failed"].append(
                    {"adapter": name, "capability": cap,
                     "reason": f"{type(e).__name__}"})
                continue
            for cand in cands:
                cand["capability"] = cap
                key = f"{cand.get('source')}:{cand.get('endpoint')}"
                prior = st["candidates"].get(key)
                if prior:
                    cand["first_seen"] = prior.get("first_seen")
                rec = qualify_candidate(cand, fetch, probe)
                st["candidates"][key] = rec
                summary["discovered"] += 1
                store.record_event(None, "candidate_discovered",
                                   capability=cap,
                                   source=cand.get("source"),
                                   status=CANDIDATE_STATUS,
                                   endpoint_reachable=rec[
                                       "endpoint_reachable"],
                                   scout=True)
                if rec["endpoint_reachable"] and rec["card_valid"]:
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
    opt-out action."""
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
    store.record_event(None, "supplier_contacted", capability=cap,
                       scout=True, disclosed_identity=True)
    try:
        send(endpoint, message)
    except Exception as e:  # noqa: BLE001
        return {"contacted": True, "delivered": False,
                "reason": f"send_failed: {type(e).__name__}"}
    return {"contacted": True, "delivered": True}
