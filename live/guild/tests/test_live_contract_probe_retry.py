"""The live contract probe distinguishes deploy transients from real drift."""
from __future__ import annotations

import importlib.util
import io
import pathlib
import urllib.error

import pytest


REPO = pathlib.Path(__file__).resolve().parents[3]
PROBE_PATH = REPO / "live" / "scripts" / "live_contract_probe.py"
SPEC = importlib.util.spec_from_file_location("live_contract_probe_retry_test", PROBE_PATH)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class _Response(io.BytesIO):
    status = 200


def test_get_retries_gateway_swap_then_returns_response(monkeypatch):
    calls = []
    sleeps = []

    def urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        if len(calls) < 3:
            raise urllib.error.HTTPError(
                request.full_url, 502, "Bad Gateway", {}, None)
        return _Response(b'{"ok":true}')

    monkeypatch.setattr(probe.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(probe.time, "sleep", sleeps.append)
    with probe.get("https://service.example/health", attempts=4,
                   retry_interval=0.25) as response:
        assert response.read() == b'{"ok":true}'
    assert len(calls) == 3
    assert sleeps == [0.25, 0.25]


def test_get_does_not_retry_semantic_http_failure(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(
            request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(probe.urllib.request, "urlopen", urlopen)
    with pytest.raises(urllib.error.HTTPError) as exc:
        probe.get("https://service.example/missing", attempts=6,
                  retry_interval=0)
    assert exc.value.code == 404
    assert calls == ["https://service.example/missing"]
