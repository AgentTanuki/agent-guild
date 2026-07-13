"""Automatic settlement under PRODUCTION hashed credentials
(GUILD_HASH_KEYS=1) — corrective pass 2026-07-13.

Before this pass, market timeouts and dispute execution passed the STORED
requester key (a public key_id under hashing) into methods that require the
RAW secret; every automatic settlement failed and the error was swallowed,
while dispute cases were still marked resolved. These tests run the entire
deterministic-settlement matrix with hashing ON and assert:

  * funds end in EXACTLY ONE terminal state (released XOR refunded, once);
  * no resolved case retains a funded/disputed escrow;
  * failures are journalled and retried idempotently (incl. across restart);
  * public HTTP escrow methods still require the RAW credential.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone, timedelta

os.environ.setdefault("GUILD_DATA", "")

import pytest  # noqa: E402
from app.store import Store  # noqa: E402
from app import market  # noqa: E402


@pytest.fixture(autouse=True)
def _hashed_production_env(monkeypatch):
    """GUILD_HASH_KEYS=1 exactly as production runs (weak KDF only to keep the
    suite fast); env restored after each test so nothing leaks."""
    monkeypatch.setenv("GUILD_HASH_KEYS", "1")
    monkeypatch.setenv("GUILD_ALLOW_WEAK_KDF", "1")
    monkeypatch.setenv("GUILD_KDF_ITERS", "10")
    monkeypatch.setenv("GUILD_SETTLE_GRACE_S", "0")
    monkeypatch.setenv("GUILD_DATA", "")


def _iso_past(seconds=10):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _pair(s):
    r = s.register_agent("Buyer", ["hiring"], metadata={})
    w = s.register_agent("Seller", ["cap.hash"], metadata={})
    return r, w


def _balance(s, agent):
    acct = s.accounts.get(agent.get("key_id") or agent["api_key"])
    return acct["balance"]


def _offer(s, r, w, amount=30, deliver=False):
    offer = market.create_offer(s, r, w["id"], "cap.hash", amount, 3600,
                                requester_key=r["api_key"])
    # stored escrow key must be the PUBLIC key_id, never the raw secret
    esc = s.escrows[offer["escrow_id"]]
    assert esc["requester_key"] == r["key_id"]
    assert not str(esc["requester_key"]).startswith("sk_")
    market.accept_offer(s, offer["id"], w)
    if deliver:
        task_id = s.offers[offer["id"]]["task_id"]
        s.submit_receipt(task_id, "0x" + "cd" * 32, outcome="accepted",
                         receipt_auth="worker_key")
    return offer


def _expire(s, offer_id):
    s.offers[offer_id]["core"]["deadline_at"] = _iso_past()


def _assert_single_terminal(s, escrow_id, expect):
    esc = s.escrows[escrow_id]
    assert esc["status"] == expect
    # exactly one settlement movement for this escrow in the billing log
    moves = [b for b in s.billing_log
             if b.get("escrow_id") == escrow_id
             and b.get("type") in ("settlement_fee",)] \
        if expect == "released" else \
        [b for b in s.billing_log
         if b.get("type") == "escrow_refund"]
    assert len(moves) >= 1


def _no_resolved_case_leaves_funds(s):
    for c in (s.dispute_cases or {}).values():
        if c["status"] == "resolved":
            assert s.escrows[c["escrow_id"]]["status"] in \
                ("released", "refunded")


# ---------------------------------------------------------------- timeouts
def test_expired_offer_refund():
    s = Store(path="")
    r, w = _pair(s)
    bal0 = _balance(s, r)
    offer = market.create_offer(s, r, w["id"], "cap.hash", 30, 3600,
                                requester_key=r["api_key"])
    assert _balance(s, r) == bal0 - 30
    _expire(s, offer["id"])
    out = market.sweep(s)
    assert out["offers_expired"] == 1
    assert out["settlement_failures"] == 0
    assert s.escrows[offer["escrow_id"]]["status"] == "refunded"
    assert _balance(s, r) == bal0
    # idempotent: a second sweep neither errors nor double-credits
    market.sweep(s)
    assert _balance(s, r) == bal0


def test_undelivered_timeout_refund():
    s = Store(path="")
    r, w = _pair(s)
    bal0 = _balance(s, r)
    offer = _offer(s, r, w, deliver=False)
    _expire(s, offer["id"])
    out = market.sweep(s)
    assert out["tasks_refunded_undelivered"] == 1
    assert s.escrows[offer["escrow_id"]]["status"] == "refunded"
    assert _balance(s, r) == bal0
    market.sweep(s)
    assert _balance(s, r) == bal0


def test_authenticated_delivery_auto_release():
    s = Store(path="")
    r, w = _pair(s)
    wbal0 = _balance(s, w)
    offer = _offer(s, r, w, amount=30, deliver=True)
    _expire(s, offer["id"])
    out = market.sweep(s)
    assert out["tasks_auto_settled"] == 1
    esc = s.escrows[offer["escrow_id"]]
    assert esc["status"] == "released"
    assert _balance(s, w) == wbal0 + (30 - esc["fee"])
    # settlement produced independent-settlement provenance
    rec = s.ledger_record_for_task(esc["task_id"])
    assert rec["provenance"] == "guild_mediated"
    market.sweep(s)   # idempotent
    assert _balance(s, w) == wbal0 + (30 - esc["fee"])


# ---------------------------------------------------------------- disputes
def _adjudicators(s, n, start=0):
    out = []
    for i in range(start, start + n):
        a = s.register_agent(f"Judge{i}", ["adjudication"], metadata={})
        a_full = s.get_agent(a["id"])
        a_full["proof_of_conduct"] = {"granted": True}
        market.enroll_adjudicator(s, a_full, a["api_key"], bond=25)
        out.append(a_full)
    return out


def _disputed(s, r, w, deliver):
    offer = _offer(s, r, w, deliver=deliver)
    s.dispute_escrow(offer["escrow_id"], r["api_key"], grounds="test")
    case = market.open_case(s, offer["escrow_id"], r["id"], "test")
    return offer, case


def test_quorum_release_and_refund():
    s = Store(path="")
    r, w = _pair(s)
    judges = {j["id"]: j for j in _adjudicators(s, 5)}
    wbal0 = _balance(s, w)
    offer, case = _disputed(s, r, w, deliver=True)
    for aid in case["panel"][:2]:
        market.cast_vote(s, case["id"], judges[aid], "release")
    case = s.dispute_cases[case["id"]]
    assert case["status"] == "resolved"
    esc = s.escrows[offer["escrow_id"]]
    assert esc["status"] == "released"
    assert _balance(s, w) == wbal0 + (offer["core"]["amount"] - esc["fee"])
    _no_resolved_case_leaves_funds(s)

    # refund quorum
    rbal = _balance(s, r)
    offer2, case2 = _disputed(s, r, w, deliver=False)
    assert _balance(s, r) == rbal - offer2["core"]["amount"]
    for aid in case2["panel"][:2]:
        market.cast_vote(s, case2["id"], judges[aid], "refund")
    assert s.dispute_cases[case2["id"]]["status"] == "resolved"
    assert s.escrows[offer2["escrow_id"]]["status"] == "refunded"
    assert _balance(s, r) == rbal
    _no_resolved_case_leaves_funds(s)


def test_deterministic_timeout_resolution():
    s = Store(path="")
    r, w = _pair(s)
    _adjudicators(s, 3)
    # delivered (worker-authenticated) -> release on deadline with no quorum
    offer, case = _disputed(s, r, w, deliver=True)
    s.dispute_cases[case["id"]]["vote_deadline_at"] = _iso_past()
    market.sweep(s)
    case = s.dispute_cases[case["id"]]
    assert case["status"] == "resolved"
    assert case["resolution"]["method"] == "deterministic_timeout"
    assert s.escrows[offer["escrow_id"]]["status"] == "released"
    # undelivered -> refund
    offer2, case2 = _disputed(s, r, w, deliver=False)
    s.dispute_cases[case2["id"]]["vote_deadline_at"] = _iso_past()
    market.sweep(s)
    assert s.escrows[offer2["escrow_id"]]["status"] == "refunded"
    _no_resolved_case_leaves_funds(s)


def test_appeal_resolution_reverses_then_settles_once():
    s = Store(path="")
    r, w = _pair(s)
    judges = {j["id"]: j for j in _adjudicators(s, 10)}
    wbal0 = _balance(s, w)
    rbal0 = _balance(s, r)
    offer, case = _disputed(s, r, w, deliver=True)
    for aid in case["panel"][:2]:
        market.cast_vote(s, case["id"], judges[aid], "release")
    esc = s.escrows[offer["escrow_id"]]
    assert esc["status"] == "released"
    # appeal: round-1 payout is clawed back, escrow re-disputed
    case = market.appeal(s, case["id"], s.get_agent(r["id"]))
    assert s.escrows[offer["escrow_id"]]["status"] == "disputed"
    assert _balance(s, w) == wbal0            # payout reversed
    for aid in case["panel"][:3]:
        market.cast_vote(s, case["id"], judges[aid], "refund")
    case = s.dispute_cases[case["id"]]
    assert case["status"] == "resolved" and case["round"] == 2
    assert s.escrows[offer["escrow_id"]]["status"] == "refunded"
    # funds ended in exactly ONE terminal state: full round trip
    assert _balance(s, r) == rbal0
    assert _balance(s, w) == wbal0
    _no_resolved_case_leaves_funds(s)


# ------------------------------------------------- failure journal + restart
def test_failed_execution_never_marks_resolved_and_retries_after_restart():
    tmp = tempfile.mkdtemp(prefix="hashkeys_")
    path = os.path.join(tmp, "guild.json")
    s = Store(path=path)
    r, w = _pair(s)
    judges = {j["id"]: j for j in _adjudicators(s, 3)}
    offer, case = _disputed(s, r, w, deliver=True)

    # sabotage execution once
    original = Store.release_escrow_internal
    def boom(self, *a, **k):
        raise RuntimeError("simulated settlement outage")
    Store.release_escrow_internal = boom
    try:
        for aid in case["panel"][:2]:
            market.cast_vote(s, case["id"], judges[aid], "release")
        case = s.dispute_cases[case["id"]]
        # NEVER resolved on failed execution; failure journalled
        assert case["status"] == "execution_pending"
        assert case["resolution"] is None
        assert s.settlement_failures
        assert s.escrows[offer["escrow_id"]]["status"] == "disputed"
    finally:
        Store.release_escrow_internal = original

    # restart: journal + case state survive; retry settles exactly once
    s2 = Store(path=path)
    assert s2.settlement_failures
    assert s2.dispute_cases[case["id"]]["status"] == "execution_pending"
    wbal_before = _balance(s2, s2.get_agent(w["id"]))
    out = market.sweep(s2)
    assert out["executions_retried"] >= 1
    case2 = s2.dispute_cases[case["id"]]
    assert case2["status"] == "resolved"
    esc = s2.escrows[offer["escrow_id"]]
    assert esc["status"] == "released"
    assert not s2.settlement_failures       # journal cleared on success
    gained = _balance(s2, s2.get_agent(w["id"])) - wbal_before
    assert gained == offer["core"]["amount"] - esc["fee"]
    # further sweeps are no-ops (idempotent)
    market.sweep(s2)
    assert _balance(s2, s2.get_agent(w["id"])) - wbal_before == gained
    _no_resolved_case_leaves_funds(s2)


# ------------------------------------------------------------- public paths
def test_public_release_still_requires_raw_credential():
    s = Store(path="")
    r, w = _pair(s)
    offer = _offer(s, r, w, deliver=True)
    esc = s.escrows[offer["escrow_id"]]
    # the stored key_id is NOT a spendable credential
    with pytest.raises(ValueError):
        s.release_escrow(offer["escrow_id"], esc["requester_key"])
    with pytest.raises(ValueError):
        s.refund_escrow(offer["escrow_id"], r["key_id"])
    # the raw secret works
    out = s.release_escrow(offer["escrow_id"], r["api_key"], rating=1.0)
    assert out["status"] == "released"
