"""Meaningful input and transport edge cases around the real business handlers."""

import asyncio
import json
import time

import httpx
import pytest

from sdk.integrations.agently import GuildActions
from sdk.integrations.agently.local_fixture import TARGET, observed, passport_request
from sdk.integrations.agently.transport import Unavailable, decode
from sdk.integrations.agently.validation import InvalidInput, public_url


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1",
        "http://8.8.8.8",
        "http://2130706433",
        "http://0x7f.1",
        "http://[::1]",
        "ftp://example.org",
        "https://a.local",
        "https://a.invalid",
        "https://localhost",
        "https://a.test",
        "https://example.org.",
        "https://user:pass@example.org",
        "https://example.org/#",
        "https://example.org:99999",
        "https://example.org\\@localhost",
        "https://exämple.org",
        "https://example.org/\nsecret",
        "https://example.org/%FF#x",
    ],
)
def test_lexical_url_rejections(url):
    with pytest.raises(InvalidInput):
        public_url(url)


def test_exact_direct_selection_and_optional_host_restriction():
    target = "http://New-Agent.example.org:8443/mcp?x=%2F&public=yes"
    assert public_url(target) == target
    assert public_url(target, frozenset({"new-agent.example.org"})) == target
    with pytest.raises(InvalidInput, match="host_not_allowed"):
        public_url(target, frozenset())


@pytest.mark.parametrize(
    "options",
    [
        {"timeout": True},
        {"timeout": 0},
        {"timeout": float("inf")},
        {"allowed_hosts": "a.org"},
        {"allowed_hosts": ["*.org"]},
        {"max_passport_age_seconds": True},
    ],
)
def test_invalid_host_configuration_is_explicit(options):
    with pytest.raises(InvalidInput):
        GuildActions(**options)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["subclass", "cycle", "unsafe_key", "integer", "deep", "large", "surrogate", "tuple"])
async def test_credential_json_guard_before_http(kind):
    calls = []

    def no_transport():
        calls.append(True)
        raise AssertionError("input should have been rejected")

    request = passport_request()
    credential = request["credential"]
    if kind == "subclass":

        class Hostile(dict):
            def items(self):
                raise AssertionError("must not invoke a custom method")

        request["credential"] = Hostile(credential)
    elif kind == "cycle":
        credential["self"] = credential
    elif kind == "unsafe_key":
        credential["credentialSubject"]["__proto__"] = {}
    elif kind == "integer":
        credential["credentialSubject"]["number"] = 2**60
    elif kind == "deep":
        item = credential
        for _ in range(18):
            item["child"] = {}
            item = item["child"]
    elif kind == "large":
        credential["credentialSubject"]["data"] = "x" * 32768
    elif kind == "surrogate":
        credential["credentialSubject"]["data"] = "\ud800"
    else:
        credential["credentialSubject"]["data"] = (1, 2)
    result = await GuildActions(transport_factory=no_transport).verify_public_passport(request)
    assert result["status"] == "rejected" and result["verified"] is False and calls == []


@pytest.mark.parametrize(
    "body", [b'{"a":1,"a":2}', b'{"x":NaN}', b'{"x":1e999}', b"\xff", b"[" * 30 + b"0" + b"]" * 30]
)
def test_strict_response_json(body):
    with pytest.raises(Unavailable, match="invalid_json_response"):
        decode(body)


@pytest.mark.asyncio
async def test_deadline_bounds_wait_even_when_host_transport_ignores_first_cancel():
    released = asyncio.Event()
    cancelled = asyncio.Event()
    finished = asyncio.Event()

    class UncooperativeHTTP(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.set()
                await released.wait()
            finally:
                finished.set()
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                stream=httpx.ByteStream(json.dumps(observed()).encode()),
                request=request,
            )

    actions = GuildActions(timeout=0.1, transport_factory=UncooperativeHTTP)
    start = time.monotonic()
    try:
        result = await actions.preflight({"url": TARGET})
        assert result["code"] == "deadline_exceeded" and time.monotonic() - start < 0.5
        await asyncio.wait_for(cancelled.wait(), 1)
        assert not finished.is_set()  # Bounded waiting did not physically terminate this override.
    finally:
        released.set()
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.sleep(0.05)
