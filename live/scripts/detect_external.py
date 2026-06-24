#!/usr/bin/env python3
"""Agent Guild — first-genuine-external-agent detector (origin-honest).

Distribution (initiative #1) has one success metric: an agent we don't operate
actually uses the service. Detecting that is subtle, because the hosted MCP
server records ALL tool calls — ours and a real agent's alike — with the same
hardcoded `actor=mcp`, `ua=mcp/remote`. User-Agent therefore CANNOT distinguish
genuine external MCP usage from our own tests. (Tracked as an instrumentation
gap; the fix is to make MCP calls carry the caller's identity.)

So this detector keys off signals that are actually attributable:

  STRONG  — a newly-registered agent that is NOT in our baseline of known-ours
            agents, not a seed, and not one of our known script names. A real
            external agent that registers shows up here unambiguously.
  STRONG  — a non-first-party call whose User-Agent matches a real framework AND
            is NOT `mcp/remote` (i.e. a direct HTTP integration we can attribute).
  INFO    — `mcp/remote` call volume. Reported for visibility but NEVER treated
            as success, because it includes our own MCP tests and cannot be
            attributed to a third party.

The marker file is overwritten each run with the current truth, so a stale
detection self-corrects. Zero third-party dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

FRAMEWORK_RE = re.compile(
    r"(httpx|aiohttp|langchain|openai|anthropic|claude|llamaindex|crewai|autogen|"
    r"semantic-kernel|node-fetch|undici|axios|okhttp|go-http-client|reqwest|"
    r"cursor|cline|continue|windsurf|cody|dify|n8n|flowise)", re.I)
OURS_UA_RE = re.compile(r"^\s*$|curl|wget|python-urllib|Python-urllib", re.I)
OURS_NAME_RE = re.compile(
    r"(Seed-Reviewer|FirstContact|Outsider-Consumer|^Ace$|^Pro$|^Solid$|^Meh$|^Weak$|"
    r"Verified-Employer)", re.I)

# MCP clients we operate ourselves — excluded from the "genuine external" signal.
# The hosted MCP server now records the connecting client's own identity from the
# initialize handshake as `mcp:<clientName>/<version>` (see mcp_server._client_ua),
# so any mcp: client name NOT in this set is an agent we do not operate.
OURS_MCP_CLIENTS = {
    "verify",        # scripts/verify_mcp.py smoke test
    "healthcheck",   # ad-hoc liveness probes
    "fastmcp", "fastmcp-client", "mcp", "client",  # default/library client names we use
    "agent-guild", "agentguild",                    # our own first-party tooling
}


def _mcp_client(ua: str) -> str | None:
    """Return the connecting MCP client's name if `ua` is the attributable
    `mcp:<name>/<version>` form, else None. Legacy `mcp/remote` returns None."""
    if not ua.startswith("mcp:"):
        return None
    return ua[4:].split("/", 1)[0].strip().lower() or None


def _get(url, timeout=25.0):
    try:
        req = urllib.request.Request(url, headers={"accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
        return None


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    results = os.path.normpath(os.path.join(here, "..", "experiments", "results"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("GUILD_URL",
                    "https://agent-guild-5d5r.onrender.com"))
    ap.add_argument("--baseline", default=os.path.join(results, "ours_baseline.json"))
    ap.add_argument("--marker", default=os.path.join(results, "first_external.json"))
    args = ap.parse_args()
    base = args.url.rstrip("/")

    if _get(f"{base}/health") is None:
        print(f"[detect] {base} unreachable; skipping.", file=sys.stderr)
        return 2

    known_ids, known_names = set(), set()
    if os.path.exists(args.baseline):
        b = json.load(open(args.baseline))
        known_ids = set(b.get("known_ours_ids", []))
        known_names = set(b.get("known_ours_names", []))

    # STRONG signal 1: a new agent we didn't create.
    agents = _get(f"{base}/agents") or []
    new_agents = [
        a for a in agents
        if a["id"] not in known_ids and not a.get("seed")
        and a["name"] not in known_names and not OURS_NAME_RE.search(a.get("name", ""))
    ]

    # STRONG signal 2: a direct (non-MCP) framework integration we can attribute.
    feed = _get(f"{base}/instrumentation/recent?limit=500&external_only=true") or {}
    events = feed.get("events", []) if isinstance(feed, dict) else []
    direct_hits = []
    mcp_external = []          # attributable mcp:<client> calls from clients not ours
    mcp_calls = 0             # legacy/unattributable mcp/remote volume (never counted)
    for e in events:
        if e.get("first_party"):
            continue
        ua = (e.get("user_agent") or "").strip()
        client = _mcp_client(ua)
        if client is not None:
            # An MCP client that names itself in the handshake. If it isn't one of
            # ours, it's an agent we don't operate arriving over MCP — the signal
            # distribution drives, finally attributable.
            if client not in OURS_MCP_CLIENTS:
                mcp_external.append(e)
            continue
        if ua == "mcp/remote":
            mcp_calls += 1
            continue
        if ua and not OURS_UA_RE.search(ua) and FRAMEWORK_RE.search(ua):
            direct_hits.append(e)

    detected = bool(new_agents or direct_hits or mcp_external)
    state = {
        "detected": detected,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "new_nonours_agents": [{"id": a["id"], "name": a["name"],
                                "capabilities": a.get("capabilities")} for a in new_agents],
        "direct_framework_calls": direct_hits[:10],
        "external_mcp_calls": [{"client": _mcp_client(e.get("user_agent", "")),
                                "endpoint": e.get("endpoint"), "paid": e.get("paid")}
                               for e in mcp_external[:10]],
        "mcp_remote_calls_unattributable": mcp_calls,
    }
    os.makedirs(results, exist_ok=True)
    json.dump(state, open(args.marker, "w"), indent=2)

    if detected:
        print("🎉 GENUINE EXTERNAL AGENT DETECTED — verify it isn't one of ours:")
        for a in new_agents:
            print(f"   NEW AGENT  {a['name']} ({a['id']}) caps={a.get('capabilities')}")
        for e in direct_hits[:5]:
            print(f"   DIRECT CALL  ua={e.get('user_agent')!r} endpoint={e.get('endpoint')} paid={e.get('paid')}")
        for e in mcp_external[:5]:
            print(f"   MCP CLIENT   {e.get('user_agent')!r} endpoint={e.get('endpoint')} paid={e.get('paid')}")
        return 0

    print("⏳ No attributable genuine external agent yet.")
    print(f"   registered agents: {len(agents)} (all match our baseline/known scripts)")
    print(f"   unattributable MCP calls (ua=mcp/remote — INCLUDES our own tests, "
          f"not counted): {mcp_calls}")
    print("   Distribution has not yet produced a third-party agent we can attribute. "
          "Keep listing; re-check on schedule.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
