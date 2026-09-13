"""One fixed Guild request with bounded waiting and an uncompressed byte cap."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math

import httpx

ORIGIN = "https://agent-guild-5d5r.onrender.com"
USER_AGENT = "agentguild-agently-example/0.1"
MAX_RESPONSE_BYTES = 65536


class Unavailable(ValueError):
    """Fixed local transport/response failure, optionally with observed HTTP status."""

    def __init__(self, code, status=None):
        super().__init__(code)
        self.status = status


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def finite(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("non-finite number")
        return result

    def constant(_value):
        raise ValueError("non-JSON constant")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_float=finite, parse_constant=constant)

        def depth(item, level=0):
            if level > 24:
                raise ValueError("response too deeply nested")
            if isinstance(item, dict):
                for child in item.values():
                    depth(child, level + 1)
            elif isinstance(item, list):
                for child in item:
                    depth(child, level + 1)

        depth(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as error:
        raise Unavailable("invalid_json_response", 200) from error


def consume(task):
    if not task.cancelled():
        task.exception()


async def request(method, route, *, timeout, params=None, body=None, transport_factory=None):
    """Only host-owned transports can override HTTP; they must cooperate with cancellation."""
    if (method, route) not in {("GET", "/preflight"), ("POST", "/credentials/verify")}:
        raise ValueError("unsupported Guild operation")

    async def read():
        transport = transport_factory() if transport_factory else httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        async with httpx.AsyncClient(
            transport=transport, timeout=timeout, follow_redirects=False, trust_env=False
        ) as client:
            async with client.stream(
                method,
                ORIGIN + route,
                params=params,
                content=body,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Content-Type": "application/json",
                },
            ) as response:
                status = response.status_code
                if str(response.url).split("?", 1)[0] != ORIGIN + route:
                    raise Unavailable("unexpected_response_url", status)
                if status != 200:
                    raise Unavailable("unexpected_http_status", status)
                if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise Unavailable("unexpected_content_type", status)
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise Unavailable("encoded_response_not_accepted", status)
                raw = bytearray()
                async for chunk in response.aiter_raw():
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise Unavailable("response_too_large", status)
                    raw.extend(chunk)
                return decode(bytes(raw)), {
                    "http_status": status,
                    "response_bytes": len(raw),
                    "response_sha256": hashlib.sha256(raw).hexdigest(),
                }

    task = asyncio.create_task(read())
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            task.cancel()
            task.add_done_callback(consume)
            raise Unavailable("deadline_exceeded")
        return task.result()
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(consume)
        raise
    except Unavailable:
        raise
    except Exception as error:
        raise Unavailable("transport_error") from error
