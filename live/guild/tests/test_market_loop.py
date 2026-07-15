"""The machine market loop: signed offers → acceptance → delivery → settlement,
deterministic timeouts, bonded quorum adjudication with slashing and appeal,
x402 payment rails, and the /check routing gate."""
import os
import time

os.environ["GUILD_DATA"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.store import Store  # noqa: E402
from app import market  # noqa: E402
from app.crypto import verify_jcs, public_key_from_did  # noqa: E402
from app.ledger import Ledger  # noqa: E402

client = TestClient(app)


def _store_pair(amount=50):
    s = Store(path="")
    r = s.register_agent("Buyer", ["hiring"], metadata={})   # registration funds
    w = s.register_agent("Seller", ["cap.market"], metadata={})  # a starter account
    return s, r, w


def test_full_loop_signed_and_bound_and_settled():
    s, r, w = _store_pair()
    offer = market.create_offer(s, r, w["id"], "cap.market", 25, 3600,
                                terms={"format": "json"},
                                requester_key=r["api_key"])
    core = offer["core"]
    # signatures verify against the named DIDs
    assert verify_jcs(core, offer["offer_sig"], public_key_from_did(r["did"]))
    assert core["value_tier"] == "low" and core["currency"] == "credits_sandbox"
    assert core["requester_config_hash"] and core["worker_config_hash"]
    assert offer["escrow_id"]

    accepted = market.accept_offer(s, offer["id"], w)
    acc = accepted["accept"]
    assert verify_jcs(acc["core"], acc["accept_sig"], public_key_from_did(w["did"]))
    assert acc["core"]["offer_hash"] == offer["core_hash"]
    task = s.tasks[accepted["task_id"]]
    assert task["metadata"]["offer_id"] == offer["id"]
    assert task["metadata"]["value_tier"] == "low"

    # worker delivers (authenticated), buyer settles via escrow release
    s.submit_receipt(task["id"], "0x" + "ab" * 32, outcome="accepted",
                     receipt_auth="worker_key")
    out = s.release_escrow(offer["escrow_id"], r["api_key"],
                           deliverable_hash="0x" + "ab" * 32, rating=1.0)
    esc = s.escrows[offer["escrow_id"]]
    assert esc["status"] == "released"
    # settlement produced a guild_mediated (independent settlement) record
    rec = s.ledger_record_for_task(esc["task_id"])
    assert rec["provenance"] == "guild_mediated"
    assert rec["evidence"]["basis"] == "independent_settlement"


def test_unaccepted_offers_expire_and_refund():
    s, r, w = _store_pair()
    bal0 = s.get_account(r["api_key"])["balance"]
    offer = market.create_offer(s, r, w["id"], "cap.market", 10, 1,
                                requester_key=r["api_key"])
    assert s.get_account(r["api_key"])["balance"] == bal0 - 10
    time.sleep(1.2)
    out = market.sweep(s)
    assert out["offers_expired"] == 1
    assert s.offers[offer["id"]]["status"] == "expired"
    assert s.get_account(r["api_key"])["balance"] == bal0


def test_delivered_but_ignored_auto_settles(monkeypatch):
    monkeypatch.setenv("GUILD_SETTLE_GRACE_S", "0")
    s, r, w = _store_pair()
    offer = market.create_offer(s, r, w["id"], "cap.market", 10, 1,
                                requester_key=r["api_key"])
    accepted = market.accept_offer(s, offer["id"], w)
    s.submit_receipt(accepted["task_id"], "0x" + "cd" * 32, outcome="accepted",
                     receipt_auth="worker_key")
    time.sleep(1.2)
    out = market.sweep(s)
    assert out["tasks_auto_settled"] == 1
    assert s.escrows[offer["escrow_id"]]["status"] == "released"


def test_undelivered_past_grace_refunds(monkeypatch):
    monkeypatch.setenv("GUILD_SETTLE_GRACE_S", "0")
    s, r, w = _store_pair()
    bal0 = s.get_account(r["api_key"])["balance"]
    offer = market.create_offer(s, r, w["id"], "cap.market", 10, 1,
                                requester_key=r["api_key"])
    market.accept_offer(s, offer["id"], w)
    time.sleep(1.2)
    out = market.sweep(s)
    assert out["tasks_refunded_undelivered"] == 1
    assert s.get_account(r["api_key"])["balance"] == bal0


def _enroll(s, name, bond=30):
    a = s.register_agent(name, ["adjudication"], metadata={})
    a["proof_of_conduct"] = {"proof_class": "test", "verified_at": "now"}
    market.enroll_adjudicator(s, a, a["api_key"], bond)
    return a


def test_bonded_quorum_adjudication_with_slashing():
    s, r, w = _store_pair()
    adjs = [_enroll(s, f"Judge{i}") for i in range(4)]
    offer = market.create_offer(s, r, w["id"], "cap.market", 20, 3600,
                                requester_key=r["api_key"])
    accepted = market.accept_offer(s, offer["id"], w)
    s.submit_receipt(accepted["task_id"], "0x" + "ee" * 32, outcome="disputed",
                     receipt_auth="worker_key")
    s.dispute_escrow(offer["escrow_id"], r["api_key"], "bad work")
    case = market.open_case(s, offer["escrow_id"], r["id"], "bad work")
    assert len(case["panel"]) == 3
    assert r["id"] not in case["panel"] and w["id"] not in case["panel"]
    p0, p1, p2 = [next(a for a in adjs if a["id"] == aid) for aid in case["panel"]]
    market.cast_vote(s, case["id"], p0, "refund", "not delivered to spec")
    market.cast_vote(s, case["id"], p1, "release", "looks fine")
    market.cast_vote(s, case["id"], p2, "refund", "agree with refund")
    case = s.dispute_cases[case["id"]]
    assert case["status"] == "resolved"
    assert case["resolution"]["verdict"] == "refund"
    assert case["resolution"]["method"] == "adjudicator_quorum"
    # minority voter got slashed
    slashes = case["resolution"]["slashes"]
    assert len(slashes) == 1 and slashes[0]["adjudicator_id"] == p1["id"]
    assert s.adjudicators[p1["id"]]["bond"] == 15
    assert s.escrows[offer["escrow_id"]]["status"] == "refunded"
    # votes are signed and verifiable
    v = case["votes"][p0["id"]]
    assert verify_jcs(v["core"], v["sig"], public_key_from_did(p0["did"]))


def test_deterministic_timeout_fallback(monkeypatch):
    monkeypatch.setenv("GUILD_DISPUTE_WINDOW_S", "0")
    s, r, w = _store_pair()
    offer = market.create_offer(s, r, w["id"], "cap.market", 20, 3600,
                                requester_key=r["api_key"])
    accepted = market.accept_offer(s, offer["id"], w)
    # worker DID deliver with authenticated receipt → timeout releases
    s.submit_receipt(accepted["task_id"], "0x" + "ff" * 32, outcome="disputed",
                     receipt_auth="worker_key")
    s.dispute_escrow(offer["escrow_id"], r["api_key"], "grumble")
    case = market.open_case(s, offer["escrow_id"], r["id"], "grumble")
    market.maybe_resolve(s, case["id"])
    case = s.dispute_cases[case["id"]]
    assert case["status"] == "resolved"
    assert case["resolution"]["method"] == "deterministic_timeout"
    assert case["resolution"]["verdict"] == "release"
    assert s.escrows[offer["escrow_id"]]["status"] == "released"


def test_one_appeal_with_fresh_larger_panel():
    s, r, w = _store_pair()
    adjs = [_enroll(s, f"Panelist{i}") for i in range(9)]
    offer = market.create_offer(s, r, w["id"], "cap.market", 20, 3600,
                                requester_key=r["api_key"])
    accepted = market.accept_offer(s, offer["id"], w)
    s.submit_receipt(accepted["task_id"], "0x" + "aa" * 32, outcome="disputed",
                     receipt_auth="worker_key")
    s.dispute_escrow(offer["escrow_id"], r["api_key"], "dispute")
    case = market.open_case(s, offer["escrow_id"], r["id"], "dispute")
    first_panel = list(case["panel"])
    for aid in first_panel:
        if s.dispute_cases[case["id"]]["status"] != "open":
            break   # quorum can resolve before the full panel votes
        a = next(x for x in adjs if x["id"] == aid)
        market.cast_vote(s, case["id"], a, "release")
    assert s.dispute_cases[case["id"]]["status"] == "resolved"
    case = market.appeal(s, case["id"], r)
    assert case["round"] == 2 and case["status"] == "open"
    assert len(case["panel"]) == 5
    assert not (set(case["panel"]) & set(first_panel))
    with pytest.raises(ValueError):
        market.appeal(s, case["id"], r)   # appeal limit: one, final


def test_x402_v2_402_body_and_stubbed_settlement(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    from app import x402
    from app import payments as _payments
    body = x402.payment_required_body(_payments.check_request("code-review"), 10)
    assert body["x402Version"] == 2
    assert body["accepts"][0]["scheme"] == "exact"
    assert body["accepts"][0]["network"].startswith("eip155:")   # CAIP-2
    assert body["accepts"][0]["payTo"] == "0x" + "11" * 20
    assert body["accepts"][0]["amount"] == "10000"  # 10 credits @ 6dp
    # exact-resource binding: the challenge quotes the CONCRETE request
    assert "/check?capability=code-review" in body["resource"]["url"]
    assert body["sandbox"]["unit"] == "credits_sandbox"
    assert "NOT money" in body["sandbox"]["note"] or "not money" in body["sandbox"]["note"]
    assert body["v1_compat"]["status"] == "removed"
    # stub the facilitator: verify ok, settle ok (v2 shapes) — the full
    # binding/replay security suite lives in tests/test_x402_v2.py
    from tests.test_x402_v2 import SEARCH, FakeFacilitator, make_payload
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    out = x402.process_payment(make_payload(SEARCH), SEARCH, 10)
    assert out["ok"] and out["transaction"].startswith("0x")
    assert out["network"] == "eip155:84532" and out["mainnet"] is False
    assert out["recipient"] == "0x" + "11" * 20


def test_check_routing_gate_requires_verified_reachability():
    s = Store(path="")
    a = s.register_agent("RoutableWorker", ["cap.route"], metadata={})
    # declared but unverified endpoint → NOT routable
    s.set_agent_endpoint(a["id"], "https://worker.example/a2a")
    r1 = s.check("cap.route")
    assert r1["routing"]["routable"] is False
    # Guild-observed invocation verifies the endpoint → routable
    inv = s.begin_outbound_invocation(a["id"])
    s.complete_outbound_invocation(inv["invocation_id"], protocol_ok=True)
    r2 = s.check("cap.route")
    assert r2["routing"]["routable"] is True
    assert r2["routing"]["provider_id"] == a["id"]
    assert r2["routing"]["invocation_supported"] is True
