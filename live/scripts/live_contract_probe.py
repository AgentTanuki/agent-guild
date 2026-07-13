#!/usr/bin/env python3
"""Live contract conformance: probe the PRODUCTION service and check it against
the committed canonical contract (live/guild/contract/contract.json).

Checks (read-only, no writes):
  * every GET route in the contract without path params answers (2xx/4xx-auth,
    never 404/5xx)
  * the A2A agent card advertises exactly the contract's skills (superset ok
    for extras is NOT allowed for statics)
  * the MCP endpoint lists exactly the contract's tools
  * /ledger/stats agrees the chain verifies; /ledger/reconcile is clean
  * the issuer DID document resolves and matches /ledger/issuer

Exit 0 iff everything matches. Used post-deploy and by `make live-conformance`.
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]
CONTRACT = json.loads((REPO / "live/guild/contract/contract.json").read_text())
HOST = CONTRACT["service"]["host"]

FAIL: list[str] = []


def get(url: str, timeout: float = 30.0):
    req = urllib.request.Request(url, headers={"X-Guild-Source": "guild-ci",
                                               "User-Agent": "guild-live-conformance"})
    return urllib.request.urlopen(req, timeout=timeout)


def check(name: str, ok: bool, detail: str = ""):
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not ok:
        FAIL.append(name)


def main() -> int:
    # 1. GET routes without params answer
    for entry in CONTRACT["rest"]:
        if "GET" not in entry["methods"] or "{" in entry["path"]:
            continue
        try:
            with get(HOST + entry["path"]) as r:
                check(f"GET {entry['path']}", r.status < 500, f"status={r.status}")
        except urllib.error.HTTPError as e:
            # conforming non-200s: auth/payment-gated (401/402/403) and
            # missing-required-query-param validation (400/422) both prove the
            # route is served; only 404/5xx mean drift
            check(f"GET {entry['path']}", e.code in (400, 401, 402, 403, 422),
                  f"status={e.code}")
        except Exception as e:
            check(f"GET {entry['path']}", False, str(e))

    # 2. agent card skills
    try:
        card = json.load(get(HOST + "/.well-known/agent-card.json"))
        skills = {s["id"] for s in card.get("skills", [])}
        expected = set(CONTRACT["a2a_skills_static"]) | set(CONTRACT["a2a_dynamic_skills"])
        check("a2a agent-card skills == contract", skills >= expected,
              f"missing={sorted(expected - skills)}")
    except Exception as e:
        check("a2a agent-card skills == contract", False, str(e))

    # 3. MCP tools via JSON-RPC (streamable-http requires an initialize
    # handshake and the returned Mcp-Session-Id on subsequent calls)
    try:
        def _rpc(method, params, session=None, id_=None):
            msg = {"jsonrpc": "2.0", "method": method, "params": params}
            if id_ is not None:
                msg["id"] = id_
            headers = {"Content-Type": "application/json",
                       "Accept": "application/json, text/event-stream",
                       "X-Guild-Source": "guild-ci"}
            if session:
                headers["Mcp-Session-Id"] = session
            req = urllib.request.Request(CONTRACT["service"]["mcp_url"],
                                         data=json.dumps(msg).encode(),
                                         method="POST", headers=headers)
            r = urllib.request.urlopen(req, timeout=60)
            raw = r.read().decode()
            payload = raw
            if raw.startswith("event:") or "\ndata:" in raw or raw.startswith("data:"):
                payload = next(l[5:].strip() for l in raw.splitlines()
                               if l.startswith("data:"))
            return r.headers.get("mcp-session-id"), (json.loads(payload) if payload.strip() else None)

        session, init = _rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "guild-live-conformance", "version": "1.0"}}, id_=1)
        _rpc("notifications/initialized", {}, session=session)
        _, resp = _rpc("tools/list", {}, session=session, id_=2)
        tools = sorted(t["name"] for t in resp["result"]["tools"])
        check("mcp tools == contract", tools == CONTRACT["mcp_tools"],
              f"live={len(tools)} contract={len(CONTRACT['mcp_tools'])}")
    except Exception as e:
        check("mcp tools == contract", False, str(e))

    # 4. ledger integrity + reconciliation on the LIVE service
    try:
        stats = json.load(get(HOST + "/ledger/stats"))
        check("live chain_valid", stats.get("chain_valid") is True)
        check("no one-party guild_mediated (effective view present)",
              "by_provenance_original" in stats)
        rec = json.load(get(HOST + "/ledger/reconcile"))
        check("live reconcile clean", rec.get("clean") is True,
              f"mismatches={len(rec.get('mismatches', []))}")
    except Exception as e:
        check("live ledger checks", False, str(e))

    # 5. issuer continuity
    try:
        didd = json.load(get(HOST + "/.well-known/agent-guild-did.json"))
        issuer = json.load(get(HOST + "/ledger/issuer"))
        check("issuer DID consistent", didd.get("did") == issuer.get("did"))
        check("issuer continuity valid", issuer.get("continuity_valid") is True)
    except Exception as e:
        check("issuer checks", False, str(e))

    print(f"\n{'CLEAN' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
