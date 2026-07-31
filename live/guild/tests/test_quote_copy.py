"""x402 challenge copy must describe the product being sold.

Found in production 2026-07-31: a deep-preflight quote rendered with the
capability-read copy — "the safest known agent for the capability" — so an
agent asking whether ONE endpoint was safe to pay was quoted for an agent
shortlist. The operation, resource and price were all correct; only the words
were wrong, which is worse than it sounds. The challenge text is the one thing
every A2A client renders, so it is the only place a machine can decide whether
the price is worth paying. A caller shown the wrong product evaluates an offer
nobody is making, and declines it — and this lands exactly on the first blocked
boundary, qualified paid-offer exposure, where every impression counts.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import a2a_x402, payments, x402  # noqa: E402
from app.store import Store  # noqa: E402

CAPABILITY_PHRASE = "safest known agent for the capability"
DEEP_PHRASE = "DEEP endpoint trust check"


@pytest.fixture()
def store(tmp_path, monkeypatch) -> Store:
    s = Store(path=str(tmp_path / "guild.json"))
    import app.main as main_mod
    import app.a2a as a2a_mod
    monkeypatch.setattr(main_mod, "store", s)
    monkeypatch.setattr(a2a_mod, "store", s)
    monkeypatch.setattr(a2a_x402, "store", s)
    return s


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def test_deep_preflight_copy_describes_the_endpoint_check():
    copy = a2a_x402.operation_copy("deep_preflight")
    assert DEEP_PHRASE in copy
    assert CAPABILITY_PHRASE not in copy
    for promised in ("drift history", "corroboration", "allow / caution / block"):
        assert promised in copy, promised


def test_capability_copy_is_unchanged():
    copy = a2a_x402.operation_copy("best_agent")
    assert CAPABILITY_PHRASE in copy
    assert DEEP_PHRASE not in copy


def test_every_paid_operation_has_its_own_copy():
    seen = {op: a2a_x402.operation_copy(op)
            for op in ("deep_preflight", "evidence_bundle", "watch_cycle",
                       "best_agent", "signed_decision")}
    assert len(set(seen.values())) == len(seen), "two operations share copy"


def test_an_unknown_operation_does_not_invent_a_promise():
    assert (a2a_x402.operation_copy("not_a_real_operation")
            == a2a_x402.operation_copy("best_agent"))


def _challenge_text_for(preq):
    required = a2a_x402.payment_required_response(preq, preq.cost)
    return a2a_x402._challenge_text(required, None, None, preq.operation)


def test_a_deep_preflight_challenge_never_renders_capability_copy():
    text = _challenge_text_for(
        payments.deep_preflight_request("https://x.example/a2a"))
    assert DEEP_PHRASE in text
    assert CAPABILITY_PHRASE not in text
    assert "USDC on Base via x402" in text, "the price must still be stated"


def test_a_capability_challenge_never_renders_deep_copy():
    text = _challenge_text_for(payments.check_request("fact-check"))
    assert CAPABILITY_PHRASE in text
    assert DEEP_PHRASE not in text


def test_the_free_alternative_matches_the_operation():
    deep = _challenge_text_for(
        payments.deep_preflight_request("https://x.example/a2a"))
    cap = _challenge_text_for(payments.check_request("fact-check"))
    assert "'preflight: <url>'" in deep
    assert "capabilities" in cap and "'preflight: <url>'" not in cap


def test_the_v01_accepts_description_is_operation_aware():
    body = a2a_x402.payment_required_response(
        payments.deep_preflight_request("https://x.example/a2a"), 20)
    desc = body["accepts"][0]["description"]
    assert "deep_preflight" in desc
    assert "deep endpoint trust check" in desc
    assert CAPABILITY_PHRASE not in desc


def test_machine_readable_descriptions_never_leak_the_paid_RESULT():
    """The description travels inside the challenge. It must say what you are
    buying without carrying the vocabulary of the result you have not paid
    for — guarded independently by tests/test_a2a_x402.py."""
    for op in ("deep_preflight", "evidence_bundle", "watch_cycle",
               "best_agent", "signed_decision"):
        label = a2a_x402.operation_label(op)
        for leak in ("shortlist", "AGD-1", "ranked candidates"):
            assert leak not in label, f"{op} leaks {leak!r}"


def test_the_v2_resource_description_is_operation_aware():
    """HTTP and MCP 402 bodies read this — all three transports must agree."""
    deep = x402.resource_info(
        payments.deep_preflight_request("https://x.example/a2a")).description
    cap = x402.resource_info(payments.check_request("fact-check")).description
    assert "deep endpoint trust check" in deep
    assert "which agent to hire" in cap
    assert deep != cap


def test_operation_resource_and_price_dispatch_are_preserved(store):
    """Copy is the ONLY thing that moved."""
    preq = payments.deep_preflight_request("https://x.example/a2a")
    task = a2a_x402.build_payment_required_task(preq, preq.cost,
                                                actor="a2a:net:x", ua="ua")
    stored = store.x402_task_get(task["id"])
    assert stored["operation"] == "deep_preflight"
    assert stored["operation_params"]["url"] == "https://x.example/a2a"
    assert stored["credits_cost"] == preq.cost
    rebuilt = a2a_x402._preq_from_task(stored)
    assert rebuilt.operation == preq.operation
    assert rebuilt.resource_url == preq.resource_url
    assert rebuilt.request_hash == preq.request_hash


def test_the_live_a2a_deep_quote_renders_the_right_product(store, client,
                                                           monkeypatch):
    import app.a2a as a2a_mod
    monkeypatch.setattr(a2a_mod, "_x402_a2a_active", lambda: True)
    r = client.post("/a2a", headers={"user-agent": "langchain/0.2.1"}, json={
        "jsonrpc": "2.0", "id": "1", "method": "message/send",
        "params": {"message": {"role": "user", "parts": [
            {"kind": "text", "text": "deep-preflight: https://x.example/a2a"}]}}})
    assert r.status_code == 200
    blob = json.dumps(r.json())
    assert DEEP_PHRASE in blob
    assert CAPABILITY_PHRASE not in blob


def test_the_capability_quote_is_unaffected_by_the_deep_copy():
    """Deliberately NOT driven through the live endpoint: a `check:` message
    records DEMAND through app.demand, which imports the module-level store
    lazily and therefore writes to the real shared queue regardless of any
    monkeypatched store. Asserting copy is not worth polluting another test's
    state, and the challenge builder is the thing under test anyway."""
    text = _challenge_text_for(payments.check_request("fact-check"))
    assert DEEP_PHRASE not in text
    assert CAPABILITY_PHRASE in text
