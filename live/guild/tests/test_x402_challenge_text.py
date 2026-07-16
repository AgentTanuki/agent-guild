"""B2 — the A2A payment challenge must be honest IN ITS TEXT PART.

Live-telemetry regression (2026-07-15): genuine external actor
a2a:net:bba57b53… asked `check: korean-legal` five times and
`check: fact-check` twice over 20 minutes, received the payment-required
Task every time, never paid, and downgraded to a free invoke. The honest
no-supply answer existed but only in `task.status.message.metadata` — the
text part, the one field every A2A client renders, said nothing except
"payment is required".

Invariants:
  * zero-supply ask: the challenge TEXT itself says do-not-pay-yet, names
    the capability, carries the free demand_id and the zero-cost actions
    (/demand/watch, /agents/register, /demand/feed);
  * supplied ask: the challenge TEXT carries the exact dollar price derived
    from the on-chain atomic quote and the free verified-reachable count;
  * the free layer stays counts-only — no shortlist, verdict, decision or
    evidence leaks anywhere in the task;
  * metadata keeps the machine-readable blocks (`io.agent-guild/supply`
    always, `io.agent-guild/no_supply` when supply is zero) so parsing
    clients lose nothing.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import demand
from app.state import store

EXT_UA = "a2a:python-httpx/0.28.1"
PAY_TO = "0x" + "11" * 20


@pytest.fixture(autouse=True)
def _enforced(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    yield


def _cap() -> str:
    return "cap-" + uuid.uuid4().hex[:10]


def _a2a_ask(client, cap):
    return client.post("/a2a", json={
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {
            "kind": "message", "role": "user", "messageId": "m1",
            "parts": [{"kind": "text", "text": f"check: {cap}"}]}},
    }, headers={"User-Agent": EXT_UA})


def _task_text(task) -> str:
    return " ".join(p.get("text", "")
                    for p in task["status"]["message"]["parts"]
                    if p.get("kind") == "text")


def test_no_supply_challenge_text_is_honest():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        r = _a2a_ask(client, cap)
        task = r.json()["result"]
        text = _task_text(task)
        canon = demand.canonical_capability(cap)
        # the do-not-pay truth is in the TEXT, not merely metadata
        assert "Do NOT pay yet" in text
        assert canon in text
        assert demand.demand_id_for(canon) in text
        # every zero-cost action is named in the text
        assert "/demand/watch" in text
        assert "/agents/register" in text
        assert "/demand/feed" in text
        # machine-readable blocks intact
        meta = task["status"]["message"]["metadata"]
        assert meta["io.agent-guild/no_supply"]["verified_reachable"] == 0
        assert meta["io.agent-guild/supply"]["demand_recorded_free"] is True
        # no paid payload leak
        blob = json.dumps(task)
        assert "shortlist" not in blob
        assert '"decision"' not in blob
        assert "attestations" not in blob


def test_paper_only_supplier_still_warns_do_not_pay():
    """A registration with no VERIFIED reachable endpoint is paper supply —
    /check would route to nobody, so the do-not-pay warning must stay."""
    from app.main import app
    cap = _cap()
    store.register_agent(name="supplier-" + cap, capabilities=[cap],
                         metadata={})
    with TestClient(app) as client:
        r = _a2a_ask(client, cap)
        task = r.json()["result"]
        text = _task_text(task)
        assert "Do NOT pay yet" in text
        meta = task["status"]["message"]["metadata"]
        assert meta["io.agent-guild/supply"]["supplied"] == 1
        assert meta["io.agent-guild/no_supply"]["verified_reachable"] == 0


def test_supplied_challenge_text_carries_price_and_counts(monkeypatch):
    from app.main import app
    cap = _cap()
    monkeypatch.setattr(
        demand, "supply_counts",
        lambda store_, canon: {"supplied": 3, "declared_endpoint": 2,
                               "verified_reachable": 2})
    with TestClient(app) as client:
        r = _a2a_ask(client, cap)
        task = r.json()["result"]
        text = _task_text(task)
        meta = task["status"]["message"]["metadata"]
        # price in the text equals the on-chain atomic quote
        atomic = int(meta["x402.payment.required"]["accepts"][0]
                     ["maxAmountRequired"])
        usd = f"${atomic / 1_000_000:.6f}".rstrip("0").rstrip(".")
        assert usd in text
        assert "USDC on Base" in text
        # free counts-only supply fact in text + metadata
        assert "verified-reachable" in text
        sup = meta["io.agent-guild/supply"]
        assert sup["supplied"] == 3
        assert sup["verified_reachable"] == 2
        assert sup["demand_recorded_free"] is True
        # never a do-not-pay warning when paying buys a real verdict... unless
        # supply is unreachable — with 0 verified endpoints the warning stays
        ns = meta.get("io.agent-guild/no_supply")
        if ns is None:
            assert "Do NOT pay yet" not in text
        # no paid payload leak
        blob = json.dumps(task)
        assert "shortlist" not in blob
        assert '"decision"' not in blob
