"""MCP paid-trust reads: no free cross-protocol bypass, and an official-SDK
MCP client can follow the payment challenge automatically.

The paid trust reads (guild_check / guild_search / guild_best_agent /
guild_risk_score) used to record paid=false and serve the full payload for
free over MCP. They now route through the SAME shared gateway as HTTP and A2A:

  * soft-launch (enforcement off) → free on every transport (one policy);
  * enforced, no payment → a COMPLETE machine-readable x402 challenge for the
    CANONICAL HTTP resource, as an MCP tool error — never the paid payload;
  * paid via _meta['x402/payment'] → the result + a signed receipt in
    _meta['x402/payment-response'];
  * a funded api_key → SANDBOX credits (labelled, never revenue).
"""
import asyncio
import base64
import json

import pytest
import mcp.types as mt
from fastmcp import Client

from x402.mcp.types import MCP_PAYMENT_META_KEY, MCP_PAYMENT_RESPONSE_META_KEY

from app import payments, x402
from app.mcp_server import mcp as guild_mcp

PAY_TO = "0x" + "11" * 20


@pytest.fixture(autouse=True)
def _enforced_env(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    yield


def _call(tool, args, meta=None):
    async def run():
        async with Client(guild_mcp,
                          client_info=mt.Implementation(name="verify",
                                                        version="1")) as c:
            return await c.call_tool(tool, args, meta=meta, raise_on_error=False)
    return asyncio.run(run())


def test_unpaid_mcp_read_returns_challenge_not_the_payload():
    r = _call("guild_check", {"capability": "fact-check"})
    assert r.is_error
    sc = r.structured_content
    assert sc["x402Version"] == 2
    # the challenge quotes the CANONICAL HTTP resource, exactly bound
    assert "/check?capability=fact-check" in sc["resource"]["url"]
    assert sc["accepts"][0]["payTo"] == PAY_TO
    # NONE of the paid trust payload leaked
    assert "best_agent" not in sc and "shortlist" not in sc
    assert "decision" not in sc


def test_unpaid_mcp_search_and_risk_also_gated():
    r = _call("guild_search", {"capability": "x"})
    assert r.is_error and r.structured_content["x402Version"] == 2
    from app.state import store
    rec = store.register_agent(name="mcp-gate", capabilities=["x"], metadata={})
    r2 = _call("guild_risk_score", {"agent_id": rec["id"]})
    assert r2.is_error and r2.structured_content["x402Version"] == 2
    assert f"/agents/{rec['id']}/risk-score" in r2.structured_content["resource"]["url"]


def test_official_sdk_mcp_client_follows_the_challenge_automatically(monkeypatch):
    """An MCP client builds the payment with the OFFICIAL x402 SDK from the
    challenge, echoes the canonical resource, retries via the official
    _meta['x402/payment'] key, and returns with the verified result — no
    Guild-specific client code on the payment path."""
    from tests.test_x402_v2 import FakeFacilitator
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())

    # 1. unpaid call → challenge
    challenge = _call("guild_check", {"capability": "fact-check"}).structured_content
    req = challenge["accepts"][0]
    resource_url = challenge["resource"]["url"]

    # 2. build the payment payload with the OFFICIAL x402 SDK types + meta
    #    helper, echoing the exact canonical resource.
    from x402.schemas import PaymentPayload, PaymentRequirements, ResourceInfo
    import time
    import uuid
    offered = PaymentRequirements(**{k: req[k] for k in
                                     ("scheme", "network", "amount", "asset")},
                                  pay_to=req["payTo"],
                                  max_timeout_seconds=req["maxTimeoutSeconds"])
    now = time.time()
    payload = PaymentPayload(
        x402_version=2, accepted=offered,
        resource=ResourceInfo(url=resource_url, mime_type="application/json"),
        payload={"signature": "0x" + "ab" * 65, "authorization": {
            "from": "0x" + "22" * 20, "to": req["payTo"], "value": req["amount"],
            "validAfter": str(int(now - 60)), "validBefore": str(int(now + 300)),
            "nonce": "0x" + uuid.uuid4().hex + uuid.uuid4().hex}})
    from x402.mcp.utils import attach_payment_to_meta
    meta = attach_payment_to_meta({}, payload)["_meta"]

    # 3. retry with the payment in _meta — automatic follow
    r = _call("guild_check", {"capability": "fact-check"}, meta=meta)
    assert not r.is_error
    body = r.structured_content                          # the paid result
    assert body["capability"] == "fact-check"
    # 4. a signed settlement response rides the result _meta
    settle = r.meta[MCP_PAYMENT_RESPONSE_META_KEY]
    assert settle["success"] is True
    assert settle["transaction"].startswith("0x")
    exts = settle.get("extensions") or {}
    assert "offer-receipt" in exts and "io.agent-guild/evidence" in exts


def test_schema_visible_payment_argument_follows_same_gateway(monkeypatch):
    """Schema-driven MCP adapters often expose only tool arguments to the
    model.  They can retry with the exact same official PaymentPayload in the
    additive ``x402_payment`` argument; verification, binding, settlement and
    receipt semantics remain identical to the request-_meta path."""
    from tests.test_x402_v2 import FakeFacilitator, make_payload
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())

    challenge = _call(
        "guild_search", {"capability": "schema-driven"}).structured_content
    preq = payments.search_request("schema-driven", 10, 0.0)
    payload = make_payload(preq).model_dump(by_alias=True, exclude_none=True)

    r = _call("guild_search", {
        "capability": "schema-driven",
        "x402_payment": payload,
    })
    assert not r.is_error
    assert r.meta[MCP_PAYMENT_RESPONSE_META_KEY]["success"] is True
    assert r.structured_content == {"result": []}
    assert challenge["resource"]["url"] == preq.resource_url


def test_stringified_payment_argument_follows_same_gateway(monkeypatch):
    """Generic MCP/LLM adapters frequently stringify object arguments.  The
    compatibility carrier accepts that common representation, then immediately
    normalizes it to the same PaymentPayload and shared settlement path."""
    from tests.test_x402_v2 import FakeFacilitator, make_payload
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    preq = payments.check_request("stringified-carrier")
    payload = make_payload(preq).model_dump(by_alias=True, exclude_none=True)

    r = _call("guild_check", {
        "capability": "stringified-carrier",
        "x402_payment": json.dumps(payload),
    })
    assert not r.is_error
    assert r.meta[MCP_PAYMENT_RESPONSE_META_KEY]["success"] is True
    assert r.structured_content["capability"] == "stringified-carrier"


def test_payment_argument_is_visible_only_on_paid_read_tools():
    """A model can discover the retry carrier from tools/list, while free
    writes do not gain irrelevant payment fields."""
    async def run():
        return await guild_mcp._list_tools()

    tools = {t.name: t.to_mcp_tool().model_dump(by_alias=True)
             for t in asyncio.run(run())}
    for name in ("guild_check", "guild_search", "guild_best_agent",
                 "guild_risk_score"):
        props = tools[name]["inputSchema"]["properties"]
        assert "x402_payment" in props
        carrier_schema = props["x402_payment"]
        serialized = json.dumps(carrier_schema)
        assert '"object"' in serialized
        assert '"string"' in serialized
    for name in ("guild_register", "guild_attest"):
        props = tools[name]["inputSchema"]["properties"]
        assert "x402_payment" not in props


def test_conflicting_payment_carriers_fail_closed_before_settlement(monkeypatch):
    from tests.test_x402_v2 import FakeFacilitator, make_payload
    facilitator = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: facilitator)
    preq = payments.check_request("carrier-conflict")
    meta_payload = make_payload(preq)
    argument_payload = meta_payload.model_dump(by_alias=True, exclude_none=True)
    argument_payload["payload"]["authorization"]["nonce"] = "0x" + "ff" * 32
    from x402.mcp.utils import attach_payment_to_meta
    meta = attach_payment_to_meta({}, meta_payload)["_meta"]

    r = _call("guild_check", {
        "capability": "carrier-conflict",
        "x402_payment": argument_payload,
    }, meta=meta)
    assert r.is_error
    assert r.structured_content["error"] == "x402_payment_invalid"
    assert r.structured_content["reason"] == "conflicting_mcp_payment"


def test_malformed_payment_argument_never_degrades_to_an_unpaid_or_free_call():
    r = _call("guild_check", {
        "capability": "malformed-carrier",
        "x402_payment": {"x402Version": 2},
    })
    assert r.is_error
    assert r.structured_content["error"] == "x402_payment_invalid"
    assert r.structured_content["reason"] == "invalid_mcp_payment_argument"
    assert "best_agent" not in r.structured_content


def test_valid_argument_cannot_override_invalid_payment_metadata():
    from tests.test_x402_v2 import make_payload
    preq = payments.check_request("bad-meta-carrier")
    payload = make_payload(preq).model_dump(by_alias=True, exclude_none=True)
    r = _call("guild_check", {
        "capability": "bad-meta-carrier",
        "x402_payment": payload,
    }, meta={MCP_PAYMENT_META_KEY: {"x402Version": 2}})
    assert r.is_error
    assert r.structured_content["reason"] == "conflicting_mcp_payment"


def test_malformed_fallback_cannot_veto_valid_official_payment(monkeypatch):
    from tests.test_x402_v2 import FakeFacilitator, make_payload
    facilitator = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: facilitator)
    preq = payments.check_request("official-carrier-wins")
    payload = make_payload(preq)
    from x402.mcp.utils import attach_payment_to_meta
    meta = attach_payment_to_meta({}, payload)["_meta"]

    r = _call("guild_check", {
        "capability": "official-carrier-wins",
        "x402_payment": {"not": "a payment"},
    }, meta=meta)
    assert not r.is_error
    assert r.meta[MCP_PAYMENT_RESPONSE_META_KEY]["success"] is True
    assert len(facilitator.settle_calls) == 1


def test_payment_argument_is_inert_when_x402_is_disabled(monkeypatch):
    """The compatibility carrier must not activate a disabled payment rail or
    make soft-launch behavior depend on an otherwise ignored field."""
    monkeypatch.setenv("GUILD_X402_ENABLED", "0")
    plain = _call("guild_check", {"capability": "disabled-rail"})
    carried = _call("guild_check", {
        "capability": "disabled-rail",
        "x402_payment": {"not": "a payment"},
    })
    assert plain.is_error and carried.is_error
    assert carried.structured_content["resource"] == plain.structured_content["resource"]
    assert carried.structured_content["accepts"] == plain.structured_content["accepts"]
    assert carried.structured_content.get("reason") != "invalid_mcp_payment_argument"
    assert (carried.meta is None
            or MCP_PAYMENT_RESPONSE_META_KEY not in carried.meta)


def test_same_authorization_cannot_settle_once_per_carrier(monkeypatch):
    """Switching the transport carrier does not create a second payment: the
    shared replay/idempotency layer keys the signed authorization, not whether
    it arrived in request metadata or tool arguments."""
    from tests.test_x402_v2 import FakeFacilitator, make_payload
    facilitator = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: facilitator)
    preq = payments.check_request("cross-carrier-replay")
    payload = make_payload(preq)
    from x402.mcp.utils import attach_payment_to_meta
    meta = attach_payment_to_meta({}, payload)["_meta"]
    first = _call("guild_check", {
        "capability": "cross-carrier-replay",
    }, meta=meta)
    assert not first.is_error

    _call("guild_check", {
        "capability": "cross-carrier-replay",
        "x402_payment": payload.model_dump(by_alias=True, exclude_none=True),
    })
    assert len(facilitator.verify_calls) == 1
    assert len(facilitator.settle_calls) == 1


def test_paid_mcp_read_records_paid_true_not_false(monkeypatch):
    from tests.test_x402_v2 import FakeFacilitator
    monkeypatch.setattr(x402, "_facilitator", lambda: FakeFacilitator())
    from app.state import store
    _call("guild_check", {"capability": "fact-check"})   # unpaid, just a probe
    # a genuine paid call must appear as paid on the x402 rail
    preq = payments.check_request("paid-cap")
    from tests.test_x402_v2 import make_payload, SEARCH
    p = make_payload(preq)
    from x402.mcp.utils import attach_payment_to_meta
    meta = attach_payment_to_meta({}, p)["_meta"]
    r = _call("guild_check", {"capability": "paid-cap"}, meta=meta)
    assert not r.is_error
    last_paid = [e for e in store.events
                 if e.get("rail") == "x402" and e.get("transport") == "mcp"]
    assert last_paid and last_paid[-1]["paid"] is True


def test_sandbox_credits_are_labelled_never_revenue(monkeypatch):
    from app.state import store
    acct = store.create_account()
    r = _call("guild_check", {"capability": "fact-check",
                              "api_key": acct["key"]})
    # served (sandbox), explicitly labelled
    assert not getattr(r, "is_error", False)
    assert r.meta and r.meta.get("x402/settlement-unit") == "credits_sandbox"
    rev = store.escrow_summary()
    assert rev["real_settlement"]["transactions"] == 0
