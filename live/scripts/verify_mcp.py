#!/usr/bin/env python3
"""Verify the LIVE hosted remote MCP end to end (stdlib only — no install).

Speaks the MCP Streamable-HTTP / JSON-RPC handshake directly against the live
/mcp endpoint: initialize -> tools/list -> call guild_best_agent. Proves an
external agent could connect to the URL and use Agent Guild as native tools.
"""
import json, os, urllib.request

BASE = os.environ.get("GUILD_MCP_URL", "https://agent-guild-5d5r.onrender.com/mcp/")


def rpc(payload, sid=None):
    data = json.dumps(payload).encode()
    r = urllib.request.Request(BASE, data=data, method="POST")
    r.add_header("Content-Type", "application/json")
    r.add_header("Accept", "application/json, text/event-stream")
    if sid:
        r.add_header("mcp-session-id", sid)
    with urllib.request.urlopen(r, timeout=30) as resp:
        sid_out = resp.headers.get("mcp-session-id")
        body = resp.read().decode()
    obj = None
    for line in body.splitlines():
        if line.startswith("data:"):
            obj = json.loads(line[5:].strip())
            break
    return sid_out, obj


def main():
    print(f"Verifying live remote MCP → {BASE}")
    print("=" * 60)

    sid, init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "verify", "version": "1"}}})
    info = init["result"]["serverInfo"]
    print(f"[1] initialize OK → server: {info['name']} v{info['version']}  (session {sid[:8]}…)")

    rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)

    _, tl = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
    names = [t["name"] for t in tl["result"]["tools"]]
    print(f"[2] tools/list OK → {names}")

    _, cr = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "guild_best_agent",
                            "arguments": {"capability": "fact-check"}}}, sid)
    content = cr["result"]
    print(f"[3] tools/call guild_best_agent(fact-check) OK →")
    print("    " + json.dumps(content.get("structuredContent", content))[:300])

    print("\n✅ Live remote MCP works: an external agent can connect to the URL,")
    print("   list tools, and call them. This is the public keystone for adoption.")


if __name__ == "__main__":
    main()
