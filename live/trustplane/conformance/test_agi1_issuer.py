"""AGI-1 conformance, runnable against any issuer:

    pytest conformance/                       # local issuer, booted for you
    pytest conformance/ --issuer-base=https://agent-guild-5d5r.onrender.com

Each requirement is one test so CI output names exactly what an issuer
violates.
"""
from __future__ import annotations

import urllib.parse

import pytest

from suite import (check_proof_verifies, check_tamper_rejected,  # noqa: E402
                   check_validity_window, check_agd1, check_binding,
                   check_evidence_inclusion, check_feed_continuity,
                   check_feed_signatures, detect_fork)


def _assert(res):
    assert res["passed"], f"{res['check']}: {res['detail']}"


def test_i2_proof_verifies(signed_decision):
    _assert(check_proof_verifies(signed_decision))


def test_v2_tamper_rejected(signed_decision):
    _assert(check_tamper_rejected(signed_decision))


def test_i3_validity_window(signed_decision):
    _assert(check_validity_window(signed_decision))


def test_i4_agd1_contract(signed_decision):
    if signed_decision.get("decision") is None:
        pytest.skip("issuer reports no supply for this capability")
    _assert(check_agd1(signed_decision["decision"]))


def test_i6_one_counterparty_binding(signed_decision):
    _assert(check_binding(signed_decision))


def test_i7_evidence_committed_by_cited_checkpoint(signed_decision, fetch):
    def fetch_inclusion(record_id, checkpoint_index):
        q = urllib.parse.urlencode({"checkpoint_index": checkpoint_index})
        return fetch(f"/ledger/inclusion/{record_id}?{q}")
    _assert(check_evidence_inclusion(signed_decision.get("decision"),
                                     fetch_inclusion))


def test_i5_feed_continuity_and_signatures(signed_decision, fetch):
    feed = fetch("/ledger/checkpoints")["checkpoints"]
    _assert(check_feed_continuity(feed))
    _assert(check_feed_signatures(feed, signed_decision.get("issuer", "")))


def test_v4_fork_detection(fetch):
    feed = fetch("/ledger/checkpoints")["checkpoints"]
    assert feed, "empty checkpoint feed"
    latest = sorted(feed, key=lambda e: e.get("index", 0))[-1]
    _assert(detect_fork(latest, latest))
    forged = dict(latest, checkpoint=dict(latest["checkpoint"],
                                          head_hash="0" * 64))
    assert not detect_fork(latest, forged)["passed"]
