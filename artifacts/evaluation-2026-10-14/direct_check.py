#!/usr/bin/env python3
"""The simple substitute AG's preflight must beat — run it beside /preflight.

A short direct check an agent can do itself, with no third party:
  1. POST an MCP `initialize` to the URL (5 s timeout) — success if the reply
     carries serverInfo/protocolVersion;
  2. otherwise GET <origin>/.well-known/agent-card.json — success if a card
     object with `url` or `skills` comes back.
Prints `success` or `failure <detail>` using the AGPO-1 detail vocabulary
(timeout | unreachable | http_5xx | http_4xx | protocol_error | card_unavailable)
so the result can go straight into `baseline.direct_check` / `baseline.detail`.
No signature check, no identity binding, no payment-claim check — if AG adds
nothing beyond this script in your workflow, that is the finding.
"""
import json, sys, time, urllib.error, urllib.request
from urllib.parse import urlsplit

url = sys.argv[1]
t0 = time.monotonic()


def done(result, detail=""):
    ms = int((time.monotonic() - t0) * 1000)
    print(result, detail, f"({ms} ms)" if detail else f"({ms} ms)")
    sys.exit(0 if result == "success" else 1)


def mcp_initialize():
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "direct-check", "version": "0"}}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=5) as r:
        text = r.read(65536).decode("utf-8", "replace")
        return '"serverInfo"' in text or '"protocolVersion"' in text


def a2a_card():
    p = urlsplit(url)
    with urllib.request.urlopen(f"{p.scheme}://{p.netloc}/.well-known/agent-card.json",
                                timeout=5) as r:
        card = json.loads(r.read(65536).decode("utf-8", "replace"))
        return isinstance(card, dict) and bool(card.get("url") or card.get("skills"))


last = "protocol_error"
try:
    if mcp_initialize():
        done("success")
except urllib.error.HTTPError as e:
    last = "http_5xx" if e.code >= 500 else "http_4xx"
except TimeoutError:
    done("failure", "timeout")
except Exception as e:  # DNS, TLS, refused
    done("failure", f"unreachable ({type(e).__name__})")
try:
    if a2a_card():
        done("success")
    done("failure", "card_unavailable")
except urllib.error.HTTPError as e:
    done("failure", "http_5xx" if e.code >= 500 else (last if last != "protocol_error" else "card_unavailable"))
except TimeoutError:
    done("failure", "timeout")
except Exception as e:
    done("failure", f"unreachable ({type(e).__name__})")
