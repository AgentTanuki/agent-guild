"""The production release gate is a normal machine customer, not a bypass.

It self-provisions through the public trial endpoint, keeps the credential out
of durable evidence, and uses it for metered conformance reads.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


_PATH = Path(__file__).resolve().parents[2] / "scripts" / "release_gate.py"
_SPEC = importlib.util.spec_from_file_location("release_gate_under_test", _PATH)
release_gate = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(release_gate)


class _Response:
    def __init__(self, body: dict):
        self._raw = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._raw


def test_gate_self_provisions_without_privileged_header(monkeypatch):
    seen = []

    def fake_open(req, timeout):
        seen.append(req)
        return _Response({"key": "ak_ephemeral_secret", "balance": 500})

    monkeypatch.setattr(release_gate.urllib.request, "urlopen", fake_open)
    key, balance = release_gate.provision_machine_key("https://issuer.example")

    assert (key, balance) == ("ak_ephemeral_secret", 500)
    assert seen[0].full_url == "https://issuer.example/billing/trial"
    assert seen[0].get_method() == "POST"
    assert seen[0].get_header("X-api-key") is None


def test_metered_get_carries_ephemeral_machine_key(monkeypatch):
    seen = []

    def fake_open(req, timeout):
        seen.append(req)
        return _Response({"verified": True})

    monkeypatch.setattr(release_gate.urllib.request, "urlopen", fake_open)
    out = release_gate._get(
        "https://issuer.example", "/check?capability=hello",
        api_key="ak_ephemeral_secret")

    assert out == {"verified": True}
    assert seen[0].get_header("X-api-key") == "ak_ephemeral_secret"
