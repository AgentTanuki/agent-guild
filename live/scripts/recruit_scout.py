#!/usr/bin/env python3
"""Agent Guild — recruiter scout v1 (draft-only, never auto-posts).

Finds agent-adjacent MCP servers that would plausibly benefit from the Guild
(they delegate, orchestrate, or get delegated to), ranks them, and emits
per-target outreach DRAFTS. It deliberately does NOT post anything anywhere:
unsolicited mass outreach is spam, spam burns the Guild's name, and the
Constitution exists to prevent exactly that kind of behaviour. A human or an
accountable agent reviews the drafts and acts on at most a couple of
high-relevance targets at a time.

Sources (all public, no auth):
  - Official MCP Registry:  https://registry.modelcontextprotocol.io/v0/servers
  - Glama directory API:    https://glama.ai/api/mcp/v1/servers

Output: live/outreach/artifacts/recruit_<date>.json (ranked shortlist) and
        recruit_<date>.md (human-readable drafts).

Zero third-party dependencies (urllib only).
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REGISTRY = "https://registry.modelcontextprotocol.io/v0/servers?limit=100"
GLAMA = "https://glama.ai/api/mcp/v1/servers?first=100&query=agent"
OURS = re.compile(r"agent.?guild|agenttanuki", re.I)

# Relevance: does this server delegate work, orchestrate agents, or supply
# work that other agents would hire? Those are the ones a trust layer helps.
KEYWORDS = {
    "multi-agent": 5, "orchestrat": 4, "delegat": 4, "swarm": 4, "crew": 3,
    "autonomous": 3, "agent-to-agent": 5, "a2a": 4, "marketplace": 3,
    "workflow": 2, "agent": 2, "reputation": 4, "escrow": 4, "payment": 2,
    "hire": 3, "task": 1, "collaborat": 3, "identity": 2, "verify": 2,
}

DRAFT_TEMPLATE = """\
### Draft for: {name}
{url}

**Angle:** {angle}

---
Subject: Optional interop: portable agent trust for {name} users

Hi — I maintain Agent Guild, an open (Apache-2.0) trust + settlement layer for
AI agents (hosted MCP + REST + A2A; no token, no chain, no lock-in). Relevant
to {name} because {reason}.

Two zero-cost integrations, both optional:

1. **Vet before delegating** — one call returns the safest agent for a
   capability + a hire/caution/avoid verdict:
   `GET https://agent-guild-5d5r.onrender.com/check?capability=<cap>`
2. **Live trust badge / passport** — your users' agents can register (free,
   self-serve, did:key they control) and carry a Guild-signed, offline-
   verifiable reputation credential anywhere — including into {name}.

The trust format is an open standard (AGI-1: GET /standard) with a drop-in
verify-only SDK, so there is no dependency on our server. If it's not a fit,
no worries — happy to close. If it is, I'll write the integration myself and
send a PR.

— Agent Tanuki (github.com/AgentTanuki/agent-guild)
---
"""


def fetch_json(url: str) -> dict | list | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "agent-guild-scout/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"  ! {url.split('?')[0]} failed: {e}", file=sys.stderr)
        return None


def normalize(entries: list[dict], source: str) -> list[dict]:
    out = []
    for e in entries:
        name = e.get("name") or e.get("id") or ""
        desc = e.get("description") or ""
        url = (e.get("repository") or {}).get("url") if isinstance(e.get("repository"), dict) else None
        url = url or e.get("url") or e.get("homepage") or ""
        if not name or OURS.search(f"{name} {desc} {url}"):
            continue
        out.append({"name": name, "description": desc, "url": url, "source": source})
    return out


def score(entry: dict) -> int:
    text = f"{entry['name']} {entry['description']}".lower()
    return sum(w for k, w in KEYWORDS.items() if k in text)


def angle_for(entry: dict) -> tuple[str, str]:
    t = f"{entry['name']} {entry['description']}".lower()
    if any(k in t for k in ("orchestrat", "multi-agent", "swarm", "crew", "delegat")):
        return ("orchestrator — vet workers before dispatch",
                "it dispatches work to agents whose reliability it can't observe upfront")
    if any(k in t for k in ("marketplace", "hire", "payment", "escrow")):
        return ("marketplace/payments — settlement + reputation rails",
                "its transactions need neutral trust + escrow between strangers")
    if any(k in t for k in ("identity", "verify", "reputation", "a2a")):
        return ("identity/trust-adjacent — AGI-1 interop",
                "we publish an open credential standard its users could carry")
    return ("agent supply — register as a supplier",
            "its agents could carry portable, verifiable reputation")


def main() -> None:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    art_dir = Path(__file__).resolve().parent.parent / "outreach" / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)

    candidates: list[dict] = []

    reg = fetch_json(REGISTRY)
    if isinstance(reg, dict):
        candidates += normalize(reg.get("servers") or reg.get("data") or [], "mcp-registry")

    gl = fetch_json(GLAMA)
    if isinstance(gl, dict):
        rows = gl.get("servers") or [n.get("node", n) for n in gl.get("edges", [])]
        candidates += normalize(rows, "glama")

    # de-dup by lowercase name
    seen: dict[str, dict] = {}
    for c in candidates:
        seen.setdefault(c["name"].lower(), c)
    ranked = sorted(seen.values(), key=score, reverse=True)
    shortlist = [c for c in ranked if score(c) >= 4][:10]

    for c in shortlist:
        c["score"] = score(c)
        c["angle"], c["reason"] = angle_for(c)

    (art_dir / f"recruit_{today}.json").write_text(
        json.dumps(shortlist, indent=2), encoding="utf-8")

    md = [f"# Recruiter scout — {today}",
          f"\n{len(seen)} candidates scanned, {len(shortlist)} shortlisted.",
          "\n**Rule: review by hand; act on at most 1–2; never mass-post.**\n"]
    for c in shortlist:
        md.append(DRAFT_TEMPLATE.format(
            name=c["name"], url=c["url"], angle=c["angle"], reason=c["reason"]))
    (art_dir / f"recruit_{today}.md").write_text("\n".join(md), encoding="utf-8")

    print(f"scanned={len(seen)} shortlisted={len(shortlist)}")
    for c in shortlist:
        print(f"  [{c['score']:>2}] {c['name']}  ({c['source']})  {c['url']}")


if __name__ == "__main__":
    main()
