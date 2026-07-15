"""A2A x402 payments extension v0.1: no free A2A bypass, native flow.

The paid A2A trust read (`check: <capability>`) used to return the full AGD-1
decision for free. Under the SAME enforcement policy as HTTP and MCP, an
unpaid caller now receives a payment-required Task (A2A x402 extension v0.1),
and only a settled payment yields the decision + a signed receipt.
"""
import base64
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app import a2a_x402, payments, x402

PAY_TO = "0x" + "11" * 20
PAYER = "0x" + "22" * 20


@pytest.fixture(autouse=True)
def _enforced_env(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    yield


def _send(client, text=None, metadata=None, task_id=None, headers=None):
    message = {"role": "user", "parts": []}
    if text is not None:
        message["parts"].append({"kind": "text", "text": text})
    if metadata is not None:
        message["metadata"] = metadata
    if task_id is not None:
        message["taskId"] = task_id
    body = {"jsonrpc": "2.0", "id": "1", "method": "message/send",
            "params": {"message": message}}
    return client.post("/a2a", json=body,
                       headers=headers or {"X-A2A-Extensions": a2a_x402.EXTENSION_URI})


def test_agent_card_declares_the_extension_when_active(monkeypatch):
    from app.main import app
    with TestClient(app) as client:
        card = client.get("/.well-known/agent-card.json").json()
        uris = [e["uri"] for e in card["capabilities"]["extensions"]]
        assert a2a_x402.EXTENSION_URI in uris
        ext = next(e for e in card["capabilities"]["extensions"]
                   if e["uri"] == a2a_x402.EXTENSION_URI)
        assert ext["params"]["pay_to"] == PAY_TO


def test_card_omits_extension_when_soft_launch(monkeypatch):
    monkeypatch.delenv("GUILD_BILLING_ENFORCED", raising=False)
    from app.main import app
    with TestClient(app) as client:
        card = client.get("/.well-known/agent-card.json").json()
        uris = [e["uri"] for e in card["capabilities"]["extensions"]]
        assert a2a_x402.EXTENSION_URI not in uris


def test_unpaid_check_returns_payment_required_task_not_the_decision():
    from app.main import app
    with TestClient(app) as client:
        r = _send(client, "check: fact-check")
        assert r.status_code == 200
        assert r.headers.get("X-A2A-Extensions") == a2a_x402.EXTENSION_URI
        task = r.json()["result"]
        assert task["kind"] == "task"
        assert task["status"]["state"] == "input-required"
        meta = task["status"]["message"]["metadata"]
        assert meta["x402.payment.status"] == "payment-required"
        required = meta["x402.payment.required"]
        assert required["x402Version"] == 1
        acc = required["accepts"][0]
        assert acc["payTo"] == PAY_TO
        assert "/check?capability=fact-check" in acc["resource"]
        # the decision itself never leaked
        blob = json.dumps(task)
        assert "shortlist" not in blob and "AGD-1" not in blob


def _payment_metadata(required, *, amount=None, resource=None, nonce=None):
    acc = required["accepts"][0]
    now = time.time()
    return {
        "x402.payment.status": "payment-submitted",
        "x402.payment.payload": {
            "x402Version": 1, "scheme": "exact", "network": acc["network"],
            "payload": {"signature": "0x" + "ab" * 65, "authorization": {
                "from": PAYER, "to": acc["payTo"],
                "value": amount or acc["maxAmountRequired"],
                "validAfter": str(int(now - 60)),
                "validBefore": str(int(now + 300)),
                "nonce": nonce or ("0x" + uuid.uuid4().hex + uuid.uuid4().hex)}},
        },
    }


def test_full_pay_flow_completes_and_returns_signed_receipt(monkeypatch):
    from tests.test_x402_v2 import FakeFacilitator
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    from app.main import app
    with TestClient(app) as client:
        task = _send(client, "check: fact-check").json()["result"]
        task_id = task["id"]
        required = task["status"]["message"]["metadata"]["x402.payment.required"]
        r = _send(client, metadata=_payment_metadata(required), task_id=task_id)
        assert r.status_code == 200
        result = r.json()["result"]
        assert result["status"]["state"] == "completed"
        meta = result["status"]["message"]["metadata"]
        assert meta["x402.payment.status"] == "payment-completed"
        receipts = meta["x402.payment.receipts"]
        assert receipts and receipts[-1]["success"] is True
        assert receipts[-1]["transaction"].startswith("0x")
        # signed offer-receipt + Guild evidence ride the settle response
        exts = receipts[-1]["extensions"]
        assert "offer-receipt" in exts and "io.agent-guild/evidence" in exts
        # the paid decision is delivered as an artifact
        art = result["artifacts"][0]
        decision = json.loads(art["parts"][0]["text"])
        assert decision["capability"] == "fact-check"


def test_tampered_amount_submission_fails_closed(monkeypatch):
    from tests.test_x402_v2 import FakeFacilitator
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    from app.main import app
    with TestClient(app) as client:
        task = _send(client, "check: fact-check").json()["result"]
        required = task["status"]["message"]["metadata"]["x402.payment.required"]
        r = _send(client, metadata=_payment_metadata(required, amount="1"),
                  task_id=task["id"])
        result = r.json()["result"]
        assert result["status"]["state"] == "failed"
        meta = result["status"]["message"]["metadata"]
        assert meta["x402.payment.status"] == "payment-failed"
        assert meta["x402.payment.error"] == "INVALID_AMOUNT"


def test_replayed_submission_is_rejected(monkeypatch):
    from tests.test_x402_v2 import FakeFacilitator
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    from app.main import app
    with TestClient(app) as client:
        task = _send(client, "check: fact-check").json()["result"]
        required = task["status"]["message"]["metadata"]["x402.payment.required"]
        md = _payment_metadata(required)
        r1 = _send(client, metadata=md, task_id=task["id"])
        assert r1.json()["result"]["status"]["state"] == "completed"
        # a second task with the SAME payment identity
        task2 = _send(client, "check: fact-check").json()["result"]
        md2 = dict(md)
        md2["x402.payment.payload"] = md["x402.payment.payload"]
        r2 = _send(client, metadata=md2, task_id=task2["id"])
        res2 = r2.json()["result"]
        assert res2["status"]["state"] == "failed"
        assert res2["status"]["message"]["metadata"]["x402.payment.error"] \
            == "DUPLICATE_NONCE"


def test_soft_launch_check_stays_free_on_a2a(monkeypatch):
    """With enforcement off, `check:` is free on every transport — one policy."""
    monkeypatch.delenv("GUILD_BILLING_ENFORCED", raising=False)
    from app.main import app
    with TestClient(app) as client:
        r = _send(client, "check: fact-check")
        payload = json.loads(r.json()["result"]["parts"][0]["text"])
        # the free decision path (not a payment task)
        assert "capability" in payload
