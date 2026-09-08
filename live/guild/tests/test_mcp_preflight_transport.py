"""Regression for valid Streamable HTTP servers misclassified by preflight."""
import json
import socket
from urllib.parse import urlsplit

import pytest

from app import preflight, reachability as r


def initialize(**overrides):
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "serverInfo": {"name": "Example", "version": "1"}}, **overrides}


class Stream:
    def __init__(self, response, *, timeout=False):
        self.response = response
        self.timeout = timeout
        self.sent = b""
        self.closed = False

    def sendall(self, value):
        self.sent += value

    def recv(self, size):
        if not self.response and self.timeout:
            raise socket.timeout()
        chunk, self.response = self.response[:size], self.response[size:]
        return chunk

    def settimeout(self, value):
        pass

    def close(self):
        self.closed = True


@pytest.mark.parametrize("sse", [False, True])
@pytest.mark.parametrize("timeout", [False, True])
def test_one_accept_header_and_complete_reply_survives_open_stream(monkeypatch, sse, timeout):
    body = json.dumps(initialize()).encode()
    if sse:
        body = b": keepalive\r\n\r\nevent: message\r\ndata: " + body + b"\r\n\r\n"
    response = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
    response += f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
    stream = Stream(response, timeout=timeout)
    monkeypatch.setattr(r, "_connect_pinned", lambda *args: stream)
    code, parsed = r._http_request_pinned(
        "https", "example.com", socket.AF_INET, "93.184.216.34", 443,
        "/mcp?transport=http", "POST", b"{}",
        "Content-Type: application/json\r\nAccept: application/json, text/event-stream\r\n")
    lines = stream.sent.partition(b"\r\n\r\n")[0].split(b"\r\n")
    accept = [line for line in lines if line.lower().startswith(b"accept:")]
    assert accept == [b"Accept: application/json, text/event-stream"]
    assert lines[0] == b"POST /mcp?transport=http HTTP/1.1"
    assert code == 200 and r._mcp_initialize_result(parsed)
    assert stream.closed


def test_timeout_before_any_response_is_not_protocol_proof(monkeypatch):
    stream = Stream(b"", timeout=True)
    monkeypatch.setattr(r, "_connect_pinned", lambda *args: stream)
    with pytest.raises(socket.timeout):
        r._http_request_pinned("https", "example.com", socket.AF_INET,
                               "93.184.216.34", 443, "/mcp")
    assert stream.closed


@pytest.mark.parametrize("body", [
    b'<html>jsonrpc result</html>', b'{"jsonrpc":"2.0","result":{}}',
    json.dumps(initialize(id=2)).encode(),
    json.dumps(initialize(id=True)).encode(),
    json.dumps(initialize(error={"code": -32600})).encode(),
    json.dumps(initialize(result={"protocolVersion": "2025-03-26"})).encode(),
    json.dumps(initialize(result={"protocolVersion": [], "capabilities": {}, "serverInfo": {}})).encode(),
    json.dumps(initialize()).encode().replace(b"Example", b"invalid-\xff"),
    json.dumps(initialize()).encode()[:-5],
    b'data: ' + json.dumps(initialize()).encode(),  # no complete SSE event
])
def test_text_errors_wrong_ids_and_incomplete_replies_never_prove_mcp(body):
    assert not r._mcp_initialize_result(body)
    def req(path, **kw):
        return 200, body
    outcome, _, _ = r._classify(urlsplit("https://example.com/mcp"), req)
    assert outcome != r.OUTCOME_PROTOCOL_RESPONSIVE


def test_sse_notifications_and_unrelated_messages_do_not_hide_matching_result():
    body = b'data: {"jsonrpc":"2.0","method":"notifications/message"}\n\n'
    body += b'data: ' + json.dumps(initialize(id=2)).encode() + b'\n\n'
    body += b'data: ' + json.dumps(initialize()).encode() + b'\n\n'
    assert r._mcp_initialize_result(body)


@pytest.mark.parametrize("code", [401, 402, 403])
def test_auth_challenges_are_unknown_protocol_not_broken_or_proven(monkeypatch, code):
    monkeypatch.setattr(r, "_resolve_and_screen", lambda *args:
                        (True, [(socket.AF_INET, "93.184.216.34")], "ok"))
    monkeypatch.setattr(r, "_http_request_pinned", lambda *args, **kw: (code, b""))
    monkeypatch.setattr(preflight, "_probe_get", lambda *args: (404, b"", ""))
    record = r.liveness_probe("https://example.com/mcp")
    assert record["status"] == "http_responsive"
    assert record["evidence_level"] == "http_response"
    out = preflight.run("https://example.com/mcp")
    assert "protocol_handshake" in out["unknowns"]
    assert "protocol_handshake" not in out["failed"]


def test_real_initialize_request_keeps_exact_path_and_query():
    calls = []
    def req(path, **kw):
        calls.append((path, kw))
        return 200, json.dumps(initialize()).encode()
    outcome, _, _ = r._classify(urlsplit("https://example.com/mcp?region=eu"), req)
    assert outcome == r.OUTCOME_PROTOCOL_RESPONSIVE
    assert calls[0][0] == "/mcp?region=eu"
    sent = json.loads(calls[0][1]["body"])
    assert sent["method"] == "initialize"
    assert sent["params"]["capabilities"] == {}


@pytest.mark.parametrize("body", [
    b'{"status":"ok"}',
    b'{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"Unsupported version"}}',
    b'data: {"jsonrpc":"2.0","id":1,"error":{"code":-32602}}\n\n',
    b'', b'data: {"jsonrpc":"2.0","method":"notifications/message"}\n\n',
])
def test_complete_invalid_reply_fails_preflight(monkeypatch, body):
    monkeypatch.setattr(r, "_resolve_and_screen", lambda *args:
                        (True, [(socket.AF_INET, "93.184.216.34")], "ok"))
    monkeypatch.setattr(r, "_http_request_pinned", lambda *args, **kw: (200, body))
    monkeypatch.setattr(preflight, "_probe_get", lambda *args: (404, b"", ""))
    out = preflight.run("https://example.com/mcp")
    assert "protocol_handshake" in out["failed"]
    assert "protocol_handshake" not in out["unknowns"]
    assert out["verdict"] == "do_not_delegate"


@pytest.mark.parametrize("body", [b'', b'{"jsonrpc":',
    b'data: {"jsonrpc":"2.0","method":"notifications/message"}\n\n'])
def test_headers_then_stall_is_unknown_end_to_end(monkeypatch, body):
    stream = Stream(b"HTTP/1.1 200 OK\r\n\r\n" + body, timeout=True)
    monkeypatch.setattr(r, "_connect_pinned", lambda *args: stream)
    monkeypatch.setattr(r, "_resolve_and_screen", lambda *args:
                        (True, [(socket.AF_INET, "93.184.216.34")], "ok"))
    monkeypatch.setattr(preflight, "_probe_get", lambda *args: (404, b"", ""))
    out = preflight.run("https://example.com/mcp")
    assert "protocol_handshake" in out["unknowns"]
    assert "protocol_handshake" not in out["failed"]
    assert stream.closed


def test_complete_sse_reply_proves_preflight_and_closes_without_draining(monkeypatch):
    body = b'data: ' + json.dumps(initialize()).encode() + b'\n\n'
    stream = Stream(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n" + body)
    original_recv = stream.recv
    def recv(size):
        assert stream.response, "probe waited after observing the complete initialize reply"
        return original_recv(size)
    stream.recv = recv
    monkeypatch.setattr(r, "_connect_pinned", lambda *args: stream)
    monkeypatch.setattr(r, "_resolve_and_screen", lambda *args:
                        (True, [(socket.AF_INET, "93.184.216.34")], "ok"))
    monkeypatch.setattr(preflight, "_probe_get", lambda *args: (404, b"", ""))
    out = preflight.run("https://example.com/mcp")
    assert next(c["status"] for c in out["checks"] if c["check"] == "protocol_handshake") == "proven"
    assert stream.closed


def test_slow_drip_has_total_deadline_and_preserves_http_evidence(monkeypatch):
    clock = {"now": 0.0, "reads": 0}
    stream = Stream(b"HTTP/1.1 200 OK\r\n\r\n")
    def recv(size):
        clock["reads"] += 1
        clock["now"] += 0.75
        if clock["reads"] == 1:
            return stream.response
        return b":"
    stream.recv = recv
    monkeypatch.setattr(r.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(r, "_connect_pinned", lambda *args: stream)
    code, body = r._http_request_pinned("https", "example.com", socket.AF_INET,
                                        "93.184.216.34", 443, "/mcp", "POST")
    assert clock["reads"] == 4 and clock["now"] == r.PROBE_TIMEOUT_S
    assert code == 200 and body.complete is False
    assert stream.closed


@pytest.mark.parametrize("headers,body,complete", [
    (b"Content-Length: 0", b"", True),
    (b"Content-Length: 20", b"partial", False),
    (b"Transfer-Encoding: chunked", b"3\r\nabc\r\n0\r\n\r\n", True),
    (b"Transfer-Encoding: chunked", b"3\r\nabc\r\n", False),
    (b"Transfer-Encoding: chunked", b"9\r\n0\r\n\r\n", False),
])
def test_http_framing_distinguishes_complete_and_truncated(headers, body, complete):
    _, prefix = r._http_body_prefix(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n\r\n" + body, "POST")
    assert prefix.complete is complete


@pytest.mark.parametrize("version,expected", [("2026-03-26", True), ("2026-99-99", False),
                                              ("2026-3-26", False), ("latest", False)])
def test_dated_versions_without_stale_allowlist(version, expected):
    msg = initialize(id=1.0)
    msg["result"]["protocolVersion"] = version
    assert r._mcp_initialize_result(json.dumps(msg).encode()) is expected


def test_sse_single_line_ending_is_not_a_complete_event():
    assert not r._mcp_initialize_result(b'data: ' + json.dumps(initialize()).encode() + b'\n')


@pytest.mark.parametrize("address", ["100.64.0.1", "100.127.255.254", "0.1.2.3", "::ffff:100.64.0.1"])
def test_shared_and_non_global_addresses_refused(address):
    import ipaddress
    assert r._screen_ip(ipaddress.ip_address(address))[0] is False


def test_negative_chunk_length_is_rejected_without_looping():
    raw = b"-6\r\nmalformed"
    assert r._dechunk(raw) == raw
    _, prefix = r._http_body_prefix(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + raw, "POST")
    assert prefix.complete is False
