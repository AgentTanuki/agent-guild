"""Integrity regression for first-party ARD interoperability probes."""
from __future__ import annotations

import hashlib

import app.attribution as attribution


def _alias(actor: str) -> str:
    return hashlib.sha256(
        ("agent-guild/census/v1|" + actor).encode("utf-8")
    ).hexdigest()


def test_exact_actor_alias_incident_is_first_party(monkeypatch):
    actor = "http:owned-ard-audit"
    exact_incident = {
        "actor_alias_sha256": _alias(actor),
        "reason": "unit-test exact first-party actor",
    }
    monkeypatch.setattr(
        attribution,
        "KNOWN_FIRST_PARTY_INCIDENTS",
        [exact_incident, *attribution.KNOWN_FIRST_PARTY_INCIDENTS],
    )
    event = {
        "fp": False,
        "key": actor,
        "ua": "discover/0.1",
        "at": "2099-01-01T00:00:00+00:00",
    }

    assert attribution.is_genuine_external(event) is False
    assert attribution.attribution_class(event) == "first_party_incident"
    assert attribution.caller_class(event) == "AG_TEST"


def test_alias_pin_does_not_demote_another_discover_client(monkeypatch):
    exact_incident = {
        "actor_alias_sha256": _alias("http:owned-ard-audit"),
        "reason": "unit-test exact first-party actor",
    }
    monkeypatch.setattr(
        attribution,
        "KNOWN_FIRST_PARTY_INCIDENTS",
        [exact_incident],
    )
    independent = {
        "fp": False,
        "key": "http:independent-ard-client",
        "ua": "discover/0.1",
        "at": "2099-01-01T00:00:00+00:00",
    }

    assert attribution.is_genuine_external(independent) is False
    assert attribution.attribution_class(independent) == "unrecognised_external"
    assert attribution.caller_class(independent) == "EXTERNAL_UNKNOWN"
