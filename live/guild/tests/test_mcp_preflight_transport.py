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
