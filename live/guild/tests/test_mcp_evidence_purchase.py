"""A buyer can quote, pay, retain and verify evidence using only MCP.

Synthetic probes and payments only. Same paid request and replay record as HTTP.
"""
import asyncio
import copy
import json
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from fastmcp import Client
from starlette.requests import Request
from x402.mcp.types import MCP_PAYMENT_META_KEY, MCP_PAYMENT_RESPONSE_META_KEY
from x402.schemas import PaymentPayload, PaymentRequired

from app import abuse, deepcheck, main, mcp_server as server, paidcatalog, payments, state, x402
from app.store import Store
from tests.test_evidence_purchase import evidence_store, URL, AUDIENCE, verify
from tests.test_mcp_x402 import _call
from tests.test_payment_identifier import _pid, _with_pid
from tests.test_x402_v2 import FakeFacilitator, PAYER, make_payload, sig_header


@pytest.fixture
def store(evidence_store, monkeypatch):
    monkeypatch.setattr(server, "store", evidence_store)
    return evidence_store


def args():
    return {"url": URL, "audience": AUDIENCE, "ttl_seconds": 3600}


def never(*a, **kw):
    raise AssertionError("unexpected issuance, decoration or settlement")


def test_tool_schema_exposes_complete_purchase_and_free_verification():
    async def inventory():
        async with Client(server.mcp) as c:
            return {tool.name: tool for tool in await c.list_tools()}
    tools = asyncio.run(inventory())
    buy = tools["guild_evidence_bundle"]
    assert buy.annotations.readOnlyHint is False
    assert buy.annotations.idempotentHint is False  # sandbox purchases still charge
    assert set(buy.inputSchema["properties"]) == {
        "url", "audience", "ttl_seconds", "api_key", "x402_payment"}
    assert buy.inputSchema["required"] == ["url"]
    assert tools["guild_evidence_verify"].annotations.readOnlyHint is True
    offered = {op["operation"]: op for op in paidcatalog.operations()}["evidence_bundle"]
    assert offered["alternatives"]["mcp"].split("(")[0] == buy.name
    assert offered["alternatives"]["mcp_verify"].split("(")[0] in tools


def test_unpaid_quote_is_exact_and_does_not_issue(store, monkeypatch):
    monkeypatch.setattr(deepcheck, "evidence_bundle", never)
    monkeypatch.setattr(x402, "_facilitator", never)
    r = _call("guild_evidence_bundle", args())
    assert r.is_error
    preq = payments.evidence_bundle_request(URL, 3600, AUDIENCE)
    assert r.structured_content["resource"]["url"] == preq.resource_url
    assert r.structured_content["accepts"][0]["amount"] == x402.requirements(preq.cost).amount
    assert json.loads(r.content[0].text) == r.structured_content
    assert not store.ledger_records


def test_cold_mcp_client_uses_served_quote_and_verifies_delivered_artifact(store, monkeypatch):
    """The buyer builds from the wire quote, without an internal price builder.

    The facilitator and signature remain synthetic: this proves client flow,
    not a funded buyer or real settlement. Official signing has separate interop CI.
    """
    fac = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    async def journey():
        async with Client(server.mcp) as client:
            inventory = {tool.name: tool for tool in await client.list_tools()}
            tool = inventory["guild_evidence_bundle"]
            assert {"url", "audience"} <= set(tool.inputSchema["properties"])
            quoted = await client.call_tool(tool.name, args(), raise_on_error=False)
            quote = PaymentRequired.model_validate(json.loads(quoted.content[0].text))
            accepted = quote.accepts[0]
            now = int(time.time())
            payment = PaymentPayload(x402_version=2, accepted=accepted,
                resource=quote.resource, payload={"signature": "0x" + "ab" * 65,
                    "authorization": {"from": PAYER, "to": accepted.pay_to,
                        "value": accepted.amount, "validAfter": str(now - 60),
                        "validBefore": str(now + 300), "nonce": "0x" + "37" * 32}})
            payment = _with_pid(payment, _pid())
            wire = payment.model_dump(by_alias=True, exclude_none=True)
            expired = copy.deepcopy(wire)
            expired["payload"]["authorization"]["validBefore"] = str(now - 1)
            refused = await client.call_tool(tool.name, {**args(), "x402_payment": expired},
                                             raise_on_error=False)
            assert refused.is_error
            assert refused.structured_content["reason"] == "authorization_expired"
            assert not store.ledger_records and not fac.settle_calls
            paid = await client.call_tool(tool.name, {**args(), "x402_payment": wire},
                                          raise_on_error=False)
            assert not paid.is_error
            bundle = json.loads(paid.content[0].text)
            checked = await client.call_tool("guild_evidence_verify", {"bundle": bundle,
                "expected_endpoint": URL, "expected_audience": AUDIENCE})
            assert checked.structured_content["valid"] and verify(bundle)["valid"]
            assert MCP_PAYMENT_RESPONSE_META_KEY in paid.meta
            assert len(fac.settle_calls) == 1
    asyncio.run(journey())


def test_credit_purchase_returns_unchanged_signed_artifact_and_verifies_free(store, monkeypatch):
    account = store.create_account()
    store.credit(account["key"], 500, reason="synthetic test only")
    before = store.get_account(account["key"])["balance"]
    monkeypatch.setattr(server, "_with_inbox", never)
    r = _call("guild_evidence_bundle", {**args(), "api_key": account["key"]})
    assert not r.is_error
    bundle = r.structured_content
    assert json.loads(r.content[0].text) == bundle
    assert verify(bundle)["valid"]
    assert r.meta["x402/settlement-unit"] == "credits_sandbox"
    cost = payments.evidence_bundle_request(URL, 3600, AUDIENCE).cost
    assert store.get_account(account["key"])["balance"] == before - cost
    for endpoint, audience, valid in ((URL, AUDIENCE, True), (URL + "other", AUDIENCE, False),
                                     (URL, "another task", False)):
        checked = _call("guild_evidence_verify", {"bundle": bundle,
                        "expected_endpoint": endpoint, "expected_audience": audience})
        assert not checked.is_error
        assert checked.structured_content["valid"] is valid
    assert store.get_account(account["key"])["balance"] == before - cost
    tampered = copy.deepcopy(bundle)
    tampered["ledger_anchor"]["inclusion"]["path"].append({"position": "right", "hash": "0" * 64})
    assert not _call("guild_evidence_verify", {"bundle": tampered}).structured_content["valid"]


@pytest.mark.parametrize("carrier", ["metadata", "argument"])
def test_x402_purchase_recovers_across_restart_and_transports(store, monkeypatch, carrier):
    fac = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    preq = payments.evidence_bundle_request(URL, 3600, AUDIENCE)
    payment = _with_pid(make_payload(preq, cost=preq.cost), _pid())
    wire = payment.model_dump(by_alias=True, exclude_none=True)
    kwargs = {"meta": {MCP_PAYMENT_META_KEY: wire}} if carrier == "metadata" else {}
    arguments = args() if carrier == "metadata" else {**args(), "x402_payment": wire}
    r = _call("guild_evidence_bundle", arguments, **kwargs)
    assert not r.is_error, r.structured_content
    assert verify(r.structured_content)["valid"]
    assert MCP_PAYMENT_RESPONSE_META_KEY in r.meta
    assert len(fac.settle_calls) == 1
    restored = Store(path=store.path)
    for module in (state, main, server):
        monkeypatch.setattr(module, "store", restored)
    payments._inflight_reset_for_process_restart()
    monkeypatch.setattr(deepcheck, "evidence_bundle", never)
    monkeypatch.setattr(x402, "_facilitator", never)
    replay = _call("guild_evidence_bundle", arguments, **kwargs)
    assert not replay.is_error
    assert replay.meta["x402/idempotent-replay"] is True
    assert replay.content[0].text == r.content[0].text
    with TestClient(main.app) as client:
        http_replay = client.post("/evidence/bundle", json=args(),
                                  headers={"PAYMENT-SIGNATURE": sig_header(payment)})
        assert http_replay.status_code == 200
        assert http_replay.content == r.content[0].text.encode()
    altered = copy.deepcopy(wire)
    altered["payload"]["signature"] = "0x" + "cd" * 65
    rejected = _call("guild_evidence_bundle", {**args(), "x402_payment": altered})
    assert rejected.is_error
    assert rejected.structured_content["reason"] == "payment_identifier_payload_mismatch"
    assert len(fac.settle_calls) == 1


def test_http_purchase_is_recoverable_through_mcp(store, monkeypatch):
    fac = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    preq = payments.evidence_bundle_request(URL, 3600, AUDIENCE)
    payment = _with_pid(make_payload(preq, cost=preq.cost), _pid())
    with TestClient(main.app) as client:
        paid = client.post("/evidence/bundle", json=args(),
                           headers={"PAYMENT-SIGNATURE": sig_header(payment)})
    assert paid.status_code == 200
    monkeypatch.setattr(deepcheck, "evidence_bundle", never)
    monkeypatch.setattr(x402, "_facilitator", never)
    r = _call("guild_evidence_bundle", {**args(), "x402_payment": payment.model_dump(by_alias=True, exclude_none=True)})
    assert not r.is_error
    assert r.content[0].text.encode() == paid.content
    assert verify(r.structured_content)["valid"]
    assert len(fac.settle_calls) == 1


@pytest.mark.parametrize("funding", ["credits", "x402"])
def test_failed_issuance_never_charges(store, monkeypatch, funding):
    fac = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    def fail(*a, **kw):
        raise deepcheck.EvidenceIssuanceRefused("synthetic failure")
    monkeypatch.setattr(deepcheck, "evidence_bundle", fail)
    account = store.create_account()
    store.credit(account["key"], 500, reason="synthetic test only")
    before = store.get_account(account["key"])["balance"]
    if funding == "credits":
        payment_args = {"api_key": account["key"]}
    else:
        preq = payments.evidence_bundle_request(URL, 3600, AUDIENCE)
        payment_args = {"x402_payment": make_payload(preq, cost=preq.cost).model_dump(by_alias=True, exclude_none=True)}
    r = _call("guild_evidence_bundle", {**args(), **payment_args})
    assert r.is_error
    assert r.structured_content["billing"] == "not_charged"
    assert not fac.settle_calls
    assert store.get_account(account["key"])["balance"] == before


def test_unknown_key_and_wrong_audience_payment_do_not_issue(store, monkeypatch):
    monkeypatch.setattr(deepcheck, "evidence_bundle", never)
    monkeypatch.setattr(x402, "_facilitator", never)
    r = _call("guild_evidence_bundle", {**args(), "api_key": "unknown-synthetic-key"})
    assert r.is_error and r.structured_content["error"] == "unknown_billing_key"
    preq = payments.evidence_bundle_request(URL, 3600, "wrong audience")
    r = _call("guild_evidence_bundle", {**args(), "x402_payment": make_payload(preq, cost=preq.cost).model_dump(by_alias=True, exclude_none=True)})
    assert r.is_error and r.structured_content["reason"] == "resource_mismatch"


def test_error_after_settlement_never_claims_buyer_was_not_charged(store, monkeypatch):
    fac = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    original = store.record_event
    def fail_after_charge(actor, kind, **kw):
        if kind == "evidence_bundle_issued":
            raise HTTPException(503, "synthetic post-settlement event failure")
        return original(actor, kind, **kw)
    monkeypatch.setattr(store, "record_event", fail_after_charge)
    preq = payments.evidence_bundle_request(URL, 3600, AUDIENCE)
    payment = _with_pid(make_payload(preq, cost=preq.cost), _pid())
    r = _call("guild_evidence_bundle", {**args(), "x402_payment":
              payment.model_dump(by_alias=True, exclude_none=True)})
    assert r.is_error
    assert len(fac.settle_calls) == 1
    assert "not_charged" not in json.dumps(r.structured_content)
    assert "not_charged" not in r.content[0].text


def test_hosted_mcp_shares_issuance_quota_and_does_not_charge(store, monkeypatch):
    import fastmcp.server.dependencies as dependencies
    request = Request({"type": "http", "method": "POST", "path": "/mcp",
                       "headers": [], "client": ("203.0.113.42", 4242)})
    monkeypatch.setattr(dependencies, "get_http_request", lambda: request)
    monkeypatch.setenv("GUILD_ABUSE_CONTROLS", "1")
    monkeypatch.setenv("GUILD_RL_EVIDENCE_ISSUE", "0")
    account = store.create_account()
    before = account["balance"]
    monkeypatch.setattr(deepcheck, "evidence_bundle", never)
    r = _call("guild_evidence_bundle", {**args(), "api_key": account["key"]})
    assert r.is_error
    assert r.structured_content["status"] == 429
    assert r.structured_content["detail"]["bucket"] == "evidence_issue"
    assert r.structured_content["billing"] == "not_charged"
    assert store.get_account(account["key"])["balance"] == before
    abuse.reset()
