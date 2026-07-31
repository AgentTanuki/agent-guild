"""Delegation preflight + the reachability defects it uncovered.

The preflight's whole value is that it does NOT overstate. Two failure
directions are equally fatal and both are tested here:

  * OVERSTATING — reporting a check as passed when it was not performed, or
    averaging unknowns into a clean verdict. That is the badge problem.
  * UNDERSTATING — reporting a well-formed agent as broken because our own
    prober could not read it. Two real instances of this were found while
    building the endpoint and are locked below: chunked transfer-encoding, and
    a card larger than the bounded probe read.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import preflight, reachability  # noqa: E402


# --------------------------------------------------------------------------
# Reachability defects found 2026-07-31
# --------------------------------------------------------------------------
def test_dechunk_decodes_a_chunked_body():
    raw = b"1a\r\n{\"protocolVersion\": \"0.3\"}\r\n0\r\n\r\n"
    assert reachability._dechunk(raw) == b'{"protocolVersion": "0.3"}'


def test_dechunk_tolerates_truncation_at_the_read_cap():
    """The probe read is bounded, so the final chunk is routinely cut off and
    the terminating 0-chunk never arrives. We must still return what we got."""
    raw = b"20\r\n{\"protocolVersion\": \"0.3\", \"na"
    out = reachability._dechunk(raw)
    assert out.startswith(b'{"protocolVersion"')


def test_dechunk_passes_through_a_non_chunked_body():
    raw = b'{"protocolVersion": "0.3"}'
    assert reachability._dechunk(raw) == raw


def test_card_detection_survives_truncation():
    """A LARGE valid card arrives incomplete and will not json.loads. Calling
    that 'not an agent' undercounts reachability — the same error class as
    overcounting adoption, in the other direction. This is exactly why
    `verified_reachable` read 0 for every entry in the demand feed."""
    whole = json.dumps({"protocolVersion": "0.3.0", "name": "x",
                        "skills": [{"id": "a"}], "description": "y" * 500})
    truncated = whole[:200].encode()
    assert reachability._looks_like_a2a_card(whole.encode()) is True
    assert reachability._looks_like_a2a_card(truncated) is True


def test_card_detection_still_rejects_a_non_card():
    assert reachability._looks_like_a2a_card(b"<html>404</html>") is False
    assert reachability._looks_like_a2a_card(b'{"hello": "world"}') is False
    # a JSON body that merely mentions the word must not qualify
    assert reachability._looks_like_a2a_card(b'{"note": "skills are nice"}') is False


# --------------------------------------------------------------------------
# Verdict semantics — unknowns are never laundered into a pass
# --------------------------------------------------------------------------
def _fake(monkeypatch, *, probe, card_body=b"", card_code=200, root_code=200):
    monkeypatch.setattr(reachability, "liveness_probe", lambda url, **kw: probe)

    def _get(url, path, timeout_ctx=None):
        if "agent-card" in path:
            return card_code, card_body, ""
        return root_code, b"", ""

    monkeypatch.setattr(preflight, "_probe_get", _get)


def test_http_200_without_a_handshake_is_do_not_delegate(monkeypatch):
    """THE headline case: 92.9% of listed agents report healthy, 33.9%
    complete a task. A bare HTTP 200 must never read as working."""
    _fake(monkeypatch, probe={"status": "http_responsive",
                              "evidence_level": "http_response"})
    out = preflight.run("https://example.com/a2a")
    assert out["verdict"] == "do_not_delegate"
    assert "protocol_handshake" in out["failed"]


def test_unreachable_reports_downstream_checks_as_unknown_not_failed(monkeypatch):
    _fake(monkeypatch, probe={"status": "currently_unreachable",
                              "evidence_level": "none"}, card_code=0)
    out = preflight.run("https://example.com/a2a")
    assert out["verdict"] == "do_not_delegate"
    assert "protocol_handshake" in out["unknowns"]
    assert "protocol_handshake" not in out["failed"]


def test_unsigned_card_is_caution_not_a_block(monkeypatch):
    """0.8% of cards are signed. An unsigned card is the norm, so it must
    inform the caller without pretending the agent is broken."""
    card = json.dumps({"protocolVersion": "0.3", "name": "x"}).encode()
    _fake(monkeypatch, probe={"status": "recently_reachable",
                              "evidence_level": "protocol_handshake"},
          card_body=card)
    out = preflight.run("https://example.com/a2a")
    assert out["verdict"] == "delegate_with_caution"
    assert "agent_card_signed" in out["failed"]


def test_payment_claim_that_does_not_challenge_is_a_failure(monkeypatch):
    """5.7% of self-declared paid agents actually return 402."""
    card = json.dumps({"protocolVersion": "0.3", "x402": {"price": "0.01"}}).encode()
    _fake(monkeypatch, probe={"status": "recently_reachable",
                              "evidence_level": "protocol_handshake"},
          card_body=card, root_code=200)
    out = preflight.run("https://example.com/a2a")
    assert "payment_claim_holds" in out["failed"]


def test_payment_claim_that_does_challenge_passes(monkeypatch):
    card = json.dumps({"protocolVersion": "0.3", "x402": {"price": "0.01"}}).encode()
    _fake(monkeypatch, probe={"status": "recently_reachable",
                              "evidence_level": "protocol_handshake"},
          card_body=card, root_code=402)
    out = preflight.run("https://example.com/a2a")
    assert "payment_claim_holds" not in out["failed"]


def test_no_payment_claim_is_unknown_not_a_pass(monkeypatch):
    card = json.dumps({"protocolVersion": "0.3", "name": "free thing"}).encode()
    _fake(monkeypatch, probe={"status": "recently_reachable",
                              "evidence_level": "protocol_handshake"},
          card_body=card)
    out = preflight.run("https://example.com/a2a")
    assert "payment_claim_holds" in out["unknowns"]


def test_clean_verdict_still_publishes_its_unknowns(monkeypatch):
    """A pass over four unknowns is not a pass over eight checks. The counts
    must travel with the verdict so a caller can tell the difference."""
    card = json.dumps({"protocolVersion": "0.3",
                       "signatures": [{"protected": "x"}]}).encode()
    _fake(monkeypatch, probe={"status": "recently_reachable",
                              "evidence_level": "protocol_handshake"},
          card_body=card)
    out = preflight.run("https://example.com/a2a")
    assert out["verdict"] == "no_failed_checks"
    assert out["unknowns"], "a clean verdict must still declare what it could not check"
    assert "not an endorsement" in out["headline"]
    assert len(out["scored"]) + len(out["unknowns"]) == len(out["checks"])


def test_absence_of_evidence_is_not_reported_as_risk(monkeypatch):
    card = json.dumps({"protocolVersion": "0.3"}).encode()
    _fake(monkeypatch, probe={"status": "recently_reachable",
                              "evidence_level": "protocol_handshake"},
          card_body=card)
    out = preflight.run("https://example.com/a2a")
    ev = next(c for c in out["checks"] if c["check"] == "independent_evidence")
    assert ev["status"] == "unknown"
    assert "NOT evidence of risk" in ev["detail"]


def test_signature_presence_is_never_called_verification(monkeypatch):
    card = json.dumps({"protocolVersion": "0.3",
                       "signatures": [{"protected": "x"}]}).encode()
    _fake(monkeypatch, probe={"status": "recently_reachable",
                              "evidence_level": "protocol_handshake"},
          card_body=card)
    out = preflight.run("https://example.com/a2a")
    sig = next(c for c in out["checks"] if c["check"] == "agent_card_signed")
    assert "not verified here" in sig["detail"]


def test_private_and_loopback_targets_are_refused(monkeypatch):
    """SSRF: the preflight must never be usable as an internal port scanner."""
    for target in ("http://127.0.0.1:8000/a2a", "http://169.254.169.254/",
                   "http://10.0.0.5/a2a", "file:///etc/passwd"):
        out = preflight.run(target)
        assert out["verdict"] == "do_not_delegate", target


def test_preflight_never_raises_on_hostile_input():
    for target in ("", "not-a-url", "https://", "http://[::1]/", "x" * 600):
        out = preflight.run(target)
        assert "verdict" in out
