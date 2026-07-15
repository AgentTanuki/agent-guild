"""Real-adapter protocol regressions (pre-mainnet swarm completion pass).

The scout's registry adapters carried three protocol errors that fixtures
concealed:

  * A2A — fetched https://a2aregistry.org/registry.json (an HTML SPA shell),
    not the live JSON API at /api/agents;
  * MCP — GET the MCP endpoint expecting a JSON "card"; a Streamable HTTP
    MCP endpoint answers POSTed JSON-RPC, not GET. The registry manifest IS
    the discovery evidence; reachability is a bounded `initialize` probe;
  * x402 Bazaar — required the priced resource to return 200 JSON unpaid;
    a valid HTTP 402 payment challenge IS protocol reachability, and the
    Bazaar item IS the manifest.

Plus: the crude HTTP/chunk parsing corrupted legitimate chunked responses.
"""
import json
import uuid

import pytest

from app.state import store
from app.swarm import scout


def _cap():
    return "adapter-cap-" + uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# A2A registry: live JSON endpoint, {"agents": [...]} shape
# ---------------------------------------------------------------------------

def test_a2a_adapter_uses_the_live_api_agents_endpoint():
    cap = _cap()
    seen = []

    def fetch(url, **kw):
        seen.append(url)
        if url == "https://a2aregistry.org/api/agents":
            return ({"agents": [{"name": "s", "url": "https://s.example/a2a",
                                 "description": f"does {cap}",
                                 "wellKnownURI": "https://s.example/"
                                                 ".well-known/agent-card.json"}]},
                    "ok")
        return (None, "http_404")

    out = scout.adapter_a2a_registry(cap, fetch)
    assert seen == ["https://a2aregistry.org/api/agents"], (
        "the adapter must call the live JSON API, not /registry.json "
        f"(called: {seen})")
    assert len(out) == 1
    assert out[0]["endpoint"] == "https://s.example/a2a"


def test_a2a_adapter_still_accepts_a_bare_list_shape():
    """Defence: fixtures and any legacy mirror serve a bare list."""
    cap = _cap()

    def fetch(url, **kw):
        return ([{"name": "s", "url": "https://s.example/a2a",
                  "description": f"does {cap}"}], "ok")

    out = scout.adapter_a2a_registry(cap, fetch)
    assert len(out) == 1


# ---------------------------------------------------------------------------
# MCP: manifest = discovery evidence; reachability = bounded initialize probe
# ---------------------------------------------------------------------------

def test_mcp_candidate_uses_manifest_evidence_and_initialize_probe():
    cap = _cap()
    endpoint = "https://mcp.supplier.example/mcp"
    manifest = {"name": "io.example/supplier", "description": f"does {cap}",
                "remotes": [{"type": "streamable-http", "url": endpoint}]}
    cand = {"source": "mcp_registry", "endpoint": endpoint, "protocol": "mcp",
            "capability": cap, "manifest": manifest,
            "name": manifest["name"]}
    fetched = []

    def fetch(url, **kw):
        fetched.append(url)
        return (None, "should_not_be_called")

    def mcp_probe(url):
        assert url == endpoint
        return {"reachable": True, "protocol_verified": True,
                "detail": "initialize ok",
                "server_info": {"name": "supplier"}}

    rec = scout.qualify_candidate(cand, fetch, probe=None,
                                  mcp_probe=mcp_probe)
    assert rec["status"] == "discovered_unverified"
    assert rec["card_valid"] is True, (
        "the registry manifest is the discovery evidence for an MCP server")
    assert rec["endpoint_reachable"] is True
    assert not any(u.startswith(endpoint) for u in fetched), (
        "an MCP endpoint must NEVER be GETted expecting a JSON card "
        f"(fetched: {fetched})")


def test_mcp_initialize_probe_is_a_post_of_jsonrpc_initialize():
    """The probe itself must speak Streamable HTTP MCP: POST a JSON-RPC
    `initialize`, accept JSON or SSE back, bounded."""
    endpoint = "https://mcp.supplier.example/mcp"
    seen = {}

    def request(url, method="GET", body=None, headers=None, **kw):
        seen.update(url=url, method=method, body=body, headers=headers or {})
        return (200, {"content-type": "application/json"},
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "supplier", "version": "1"},
                    "capabilities": {}}}).encode(), "ok")

    out = scout.mcp_initialize_probe(endpoint, request=request)
    assert out["reachable"] is True and out["protocol_verified"] is True
    assert seen["method"] == "POST"
    body = json.loads(seen["body"])
    assert body["method"] == "initialize"
    accept = {v.strip() for v in seen["headers"].get(
        "Accept", "").split(",")}
    assert "application/json" in accept and "text/event-stream" in accept


def test_mcp_probe_auth_gated_endpoint_is_reachable_but_unverified():
    def request(url, method="GET", body=None, headers=None, **kw):
        return (401, {}, b"", "ok")

    out = scout.mcp_initialize_probe("https://mcp.x.example/mcp",
                                     request=request)
    assert out["reachable"] is True          # something MCP-shaped answered
    assert out["protocol_verified"] is False


# ---------------------------------------------------------------------------
# x402 Bazaar: the item is the manifest; a valid 402 challenge is reachability
# ---------------------------------------------------------------------------

def _bazaar_item(cap, resource):
    return {"resource": resource, "type": "http", "x402Version": 1,
            "lastUpdated": "2026-07-01T00:00:00Z",
            "description": f"does {cap}",
            "accepts": [{"scheme": "exact", "network": "eip155:8453",
                         "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                         "amount": "10000",
                         "payTo": "0x" + "aa" * 20,
                         "maxTimeoutSeconds": 300}]}


def test_x402_adapter_parses_the_real_bazaar_item_shape():
    cap = _cap()
    resource = "https://api.supplier.example/paid"

    def fetch(url, **kw):
        # documented server-side search is tried first, then the catalogue
        if "/discovery/search" in url:
            return ({"resources": [], "partialResults": False}, "ok")
        assert "/discovery/resources" in url
        return ({"items": [_bazaar_item(cap, resource)],
                 "pagination": {"limit": 100, "offset": 0, "total": 1},
                 "x402Version": 1}, "ok")

    out = scout.adapter_x402_bazaar(cap, fetch, store=store)
    assert len(out) == 1
    assert out[0]["endpoint"] == resource
    assert out[0]["wallet"] == "0x" + "aa" * 20


def test_x402_candidate_402_challenge_is_protocol_reachability():
    cap = _cap()
    resource = "https://api.supplier.example/paid"
    cand = {"source": "x402_bazaar", "endpoint": resource, "protocol": "x402",
            "capability": cap, "manifest": _bazaar_item(cap, resource),
            "name": resource}
    fetched = []

    def fetch(url, **kw):
        fetched.append(url)
        return (None, "should_not_be_called")

    def x402_probe(url, manifest=None):
        return {"reachable": True, "protocol_verified": True,
                "detail": "http_402_challenge"}

    rec = scout.qualify_candidate(cand, fetch, probe=None,
                                  x402_probe=x402_probe)
    assert rec["card_valid"] is True, "the Bazaar item IS the manifest"
    assert rec["endpoint_reachable"] is True
    assert not any(u.startswith(resource) for u in fetched), (
        "an unpaid priced resource must never be required to return 200 JSON")


def test_x402_probe_treats_a_valid_402_as_reachable_and_200_as_not_verified():
    challenge = json.dumps({"x402Version": 1, "accepts": [{
        "scheme": "exact", "network": "eip155:8453", "amount": "10000",
        "payTo": "0x" + "aa" * 20,
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}]}).encode()

    def request_402(url, method="GET", body=None, headers=None, **kw):
        return (402, {"content-type": "application/json"}, challenge, "ok")

    out = scout.x402_challenge_probe("https://api.s.example/paid",
                                     request=request_402)
    assert out["reachable"] is True and out["protocol_verified"] is True

    # a bare 402 with no parseable challenge: reachable, not protocol-verified
    def request_bare(url, method="GET", body=None, headers=None, **kw):
        return (402, {}, b"payment required", "ok")

    out = scout.x402_challenge_probe("https://api.s.example/paid",
                                     request=request_bare)
    assert out["reachable"] is True and out["protocol_verified"] is False


# ---------------------------------------------------------------------------
# correct bounded HTTP handling (chunked + content-length), not line stripping
# ---------------------------------------------------------------------------

class _FakeSock:
    def __init__(self, raw: bytes):
        self._buf = raw
        self.sent = b""

    def sendall(self, b):
        self.sent += b

    def settimeout(self, t):
        pass

    def recv(self, n):
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def close(self):
        pass


def _serve_raw(monkeypatch, raw: bytes):
    import socket as _socket
    from app import reachability
    monkeypatch.setattr(reachability, "_resolve_and_screen",
                        lambda host, port: (
                            True,
                            [(_socket.AF_INET, "93.184.216.34")], "ok"))
    sock = _FakeSock(raw)
    monkeypatch.setattr(reachability, "_connect_pinned",
                        lambda *a, **k: sock)
    return sock


def _chunked(body: bytes, sizes) -> bytes:
    out, i = b"", 0
    for n in sizes:
        chunk = body[i:i + n]
        i += n
        out += hex(len(chunk))[2:].encode() + b"\r\n" + chunk + b"\r\n"
    out += b"0\r\n\r\n"
    return out


def test_chunked_response_split_mid_token_parses_correctly(monkeypatch):
    """The old parser joined chunk fragments with newlines and stripped
    hex-looking lines — a chunk boundary inside a JSON token corrupted the
    document."""
    doc = {"agents": [{"name": "deadbeef-supplier",
                       "url": "https://s.example/a2a"}]}
    body = json.dumps(doc).encode()
    raw = (b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
           b"transfer-encoding: chunked\r\nconnection: close\r\n\r\n"
           + _chunked(body, [7, 5, len(body)]))   # splits inside '"agents"'
    _serve_raw(monkeypatch, raw)
    parsed, reason = scout.safe_fetch_json("https://reg.example/api/agents")
    assert reason == "ok", reason
    assert parsed == doc


def test_content_length_response_is_read_exactly_and_bounded(monkeypatch):
    doc = {"ok": True}
    body = json.dumps(doc).encode()
    raw = (b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
           + b"content-length: " + str(len(body)).encode() + b"\r\n\r\n"
           + body)
    _serve_raw(monkeypatch, raw)
    parsed, reason = scout.safe_fetch_json("https://reg.example/x")
    assert (parsed, reason) == (doc, "ok")


def test_oversized_response_is_refused(monkeypatch):
    big = b'{"pad": "' + b"A" * (scout.MAX_CARD_BYTES + 10) + b'"}'
    raw = (b"HTTP/1.1 200 OK\r\ncontent-length: " + str(len(big)).encode()
           + b"\r\n\r\n" + big)
    _serve_raw(monkeypatch, raw)
    parsed, reason = scout.safe_fetch_json("https://reg.example/x")
    assert parsed is None
    assert "oversize" in reason


def test_redirects_are_still_refused(monkeypatch):
    raw = (b"HTTP/1.1 302 Found\r\nlocation: http://169.254.169.254/\r\n"
           b"content-length: 0\r\n\r\n")
    _serve_raw(monkeypatch, raw)
    parsed, reason = scout.safe_fetch_json("https://reg.example/x")
    assert parsed is None
    assert "redirect" in reason
