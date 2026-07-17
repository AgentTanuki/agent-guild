"""Native MCP execution-path caller-proof verification.

The defect this closes: the caller-proof MCP transport mapping
(_meta["io.agent-guild/caller-proof"]) was DOCUMENTED but mcp_server.py never
read or verified it — so a proof-carrying MCP tool call was recorded exactly
like an anonymous one. A direct unit call to callerproof.verify_proof is NOT
sufficient evidence of wiring; these tests drive the REAL FastMCP server with
a native client and assert the verified flag + DID reach demand recording and
settlement attribution.

Contract under test (ONE verification per tools/call):
  * method     = "tools/call"
  * resource   = the exact MCP tool name
  * body       = sha256(JCS(visible tool arguments minus api_key/_meta))
  * a valid proof marks demand caller_proof_verified=True with actor did:<did>
    and flows caller_did into payments.authorize (settlement attribution);
  * tampering with the arguments after signing fails the binding;
  * replaying the same envelope fails (durable nonce);
  * absence stays UNVERIFIED (anonymous calls allowed);
  * the SAME nonce is never verified twice within one call — a single valid
    proof must serve BOTH demand recording and settlement attribution.
"""
import asyncio
import uuid

import pytest
import mcp.types as mt
from fastmcp import Client

from x402.mcp.types import MCP_PAYMENT_META_KEY, MCP_PAYMENT_RESPONSE_META_KEY

from app import callerproof, crypto, payments, walletbinding, x402, \
    x402_confirm
from app.mcp_server import mcp as guild_mcp
from app.state import store

MAINNET = "eip155:8453"
PAYER = "0x" + "22" * 20


def _cap():
    return "mcpcp-" + uuid.uuid4().hex[:8]


def _call(tool, args, meta=None):
    async def run():
        async with Client(guild_mcp,
                          client_info=mt.Implementation(name="verify",
                                                        version="1")) as c:
            return await c.call_tool(tool, args, meta=meta,
                                     raise_on_error=False)
    return asyncio.run(run())


def _did():
    priv, pub = crypto.generate_keypair()
    return priv, crypto.did_from_public_key(pub)


def _envelope(priv, did, tool, args):
    return callerproof.create_proof(
        priv, did, method="tools/call", resource=tool,
        body=callerproof.mcp_args_body(args))


@pytest.fixture(autouse=True)
def _clean_demand_residue():
    events_before = len(store.events)
    dedupe_before = dict(store.demand_dedupe)
    yield
    del store.events[events_before:]
    store.demand_dedupe.clear()
    store.demand_dedupe.update(dedupe_before)


def _demand_event(cap):
    evs = [e for e in store.events
           if e.get("type") == "capability_demand"
           and e.get("capability") == cap]
    assert evs, f"no capability_demand event for {cap}"
    return evs[-1]


# ---------------------------------------------------------------------------
# soft-launch (no enforcement): the proof must reach DEMAND recording
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool,extra", [
    ("guild_check", {}),
    ("guild_search", {"min_trust": 5.0}),
    ("guild_best_agent", {}),
])
def test_valid_proof_reaches_demand_recording(tool, extra):
    cap = _cap()
    args = {"capability": cap, **extra}
    priv, did = _did()
    env = _envelope(priv, did, tool, args)
    r = _call(tool, args, meta={callerproof.MCP_META_KEY: env})
    assert not r.is_error
    ev = _demand_event(cap)
    assert ev["caller_proof_verified"] is True, (
        f"{tool}: a valid MCP caller proof must record VERIFIED machine "
        "demand — the _meta envelope was not verified on the execution path")
    assert ev["caller_did"] == did
    assert ev["actor"] == "did:" + did


def test_tampered_arguments_fail_the_binding():
    cap = _cap()
    priv, did = _did()
    # signed over {"capability": cap} — but the call sends different args
    env = _envelope(priv, did, "guild_search", {"capability": cap})
    r = _call("guild_search", {"capability": cap, "min_trust": 40.0},
              meta={callerproof.MCP_META_KEY: env})
    assert not r.is_error
    ev = _demand_event(cap)
    assert ev["caller_proof_verified"] is False
    assert not ev.get("caller_did")


def test_replayed_envelope_fails_the_second_time():
    cap = _cap()
    priv, did = _did()
    args = {"capability": cap}
    env = _envelope(priv, did, "guild_check", args)
    r1 = _call("guild_check", args, meta={callerproof.MCP_META_KEY: env})
    assert not r1.is_error
    assert _demand_event(cap)["caller_proof_verified"] is True
    # exact replay: same envelope, same args — durable nonce must reject
    r2 = _call("guild_check", args, meta={callerproof.MCP_META_KEY: env})
    assert not r2.is_error
    ev2 = _demand_event(cap)
    assert ev2["caller_proof_verified"] is False, (
        "a replayed caller-proof envelope must never verify again")


def test_absent_meta_stays_unverified():
    cap = _cap()
    r = _call("guild_check", {"capability": cap})
    assert not r.is_error
    ev = _demand_event(cap)
    assert ev["caller_proof_verified"] is False
    assert not ev.get("caller_did")


def test_wrong_tool_name_binding_fails():
    cap = _cap()
    priv, did = _did()
    args = {"capability": cap}
    env = _envelope(priv, did, "guild_check", args)   # bound to guild_check
    r = _call("guild_search", args, meta={callerproof.MCP_META_KEY: env})
    assert not r.is_error
    assert _demand_event(cap)["caller_proof_verified"] is False


# ---------------------------------------------------------------------------
# enforced + paid: the proof must reach SETTLEMENT attribution — and the
# nonce must be verified exactly ONCE per call (demand + settlement share it)
# ---------------------------------------------------------------------------

@pytest.fixture
def _paid_mainnet(monkeypatch):
    from tests.test_x402_v2 import FakeFacilitator
    from tests.test_x402_cdp_settlement import (FAKE_KEY_ID, FAKE_SECRET,
                                                _receipt)
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_NETWORK", MAINNET)
    monkeypatch.setenv("GUILD_X402_PAY_TO", x402.MAINNET_TREASURY)
    monkeypatch.setenv("CDP_API_KEY_ID", FAKE_KEY_ID)
    monkeypatch.setenv("CDP_API_KEY_SECRET", FAKE_SECRET)
    monkeypatch.delenv("GUILD_X402_ASSET", raising=False)
    monkeypatch.delenv("GUILD_X402_FACILITATOR", raising=False)
    monkeypatch.setenv("GUILD_X402_CONFIRM_TIMEOUT", "0")
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setattr(x402, "_facilitator",
                        lambda: FakeFacilitator(network=MAINNET))
    monkeypatch.setattr(x402_confirm, "_get_receipt",
                        lambda tx, timeout=15.0: _receipt())
    monkeypatch.setattr(x402_confirm, "current_block",
                        lambda network=None, timeout=15.0: 5_000_000)
    billing_before = len(store.billing_log)
    yield
    del store.billing_log[billing_before:]


def _payment_meta(preq):
    from tests.test_x402_v2 import make_payload
    payload = make_payload(preq)
    return {MCP_PAYMENT_META_KEY: payload.model_dump(by_alias=True,
                                                     exclude_none=True)}


def _bound_did():
    priv, pub = crypto.generate_keypair()
    did = crypto.did_from_public_key(pub)
    cred = walletbinding.issue_credential(store, did=did, address=PAYER,
                                          network=MAINNET,
                                          challenge_nonce="m-" +
                                          uuid.uuid4().hex)
    return priv, did, cred


def _last_payment():
    return [b for b in store.billing_log
            if b.get("type") == "x402_payment"][-1]


def test_one_proof_serves_demand_and_settlement_attribution(_paid_mainnet):
    """THE single-verification regression: a paid guild_check carrying one
    valid proof must (a) record verified demand and (b) attribute the
    settlement — if the server verified the envelope twice, the second
    verification would be a nonce replay and attribution would fail."""
    cap = _cap()
    args = {"capability": cap}
    priv, did, cred = _bound_did()
    env = _envelope(priv, did, "guild_check", args)
    meta = {**_payment_meta(payments.check_request(cap)),
            callerproof.MCP_META_KEY: env}
    r = _call("guild_check", args, meta=meta)
    assert not r.is_error, r.structured_content
    assert r.meta[MCP_PAYMENT_RESPONSE_META_KEY]["success"] is True
    ev = _demand_event(cap)
    assert ev["caller_proof_verified"] is True
    rec = _last_payment()
    assert rec["payer_attribution"] == \
        "cryptographically_bound_machine_payer"
    assert rec["caller_did"] == did
    assert rec["wallet_binding_credential"] == cred["credential_id"]


def test_risk_score_settlement_attribution_native_mcp(_paid_mainnet):
    agent = store.register_agent(name="mcp-attr-target",
                                 capabilities=["x"], metadata={})
    args = {"agent_id": agent["id"]}
    priv, did, cred = _bound_did()
    env = _envelope(priv, did, "guild_risk_score", args)
    meta = {**_payment_meta(payments.risk_score_request(agent["id"])),
            callerproof.MCP_META_KEY: env}
    r = _call("guild_risk_score", args, meta=meta)
    assert not r.is_error, r.structured_content
    rec = _last_payment()
    assert rec["payer_attribution"] == \
        "cryptographically_bound_machine_payer"
    assert rec["caller_did"] == did


def test_tampered_proof_settles_but_stays_unverified(_paid_mainnet):
    cap = _cap()
    args = {"capability": cap}
    priv, did, cred = _bound_did()
    env = _envelope(priv, did, "guild_check", {"capability": "other-cap"})
    meta = {**_payment_meta(payments.check_request(cap)),
            callerproof.MCP_META_KEY: env}
    r = _call("guild_check", args, meta=meta)
    assert not r.is_error
    rec = _last_payment()
    assert rec["payer_attribution"] == "unverified_payer"


def test_mcp_idempotent_replay_preserves_attribution(_paid_mainnet):
    """A cached idempotent MCP replay must return the SAME attribution
    fields the original settlement carried."""
    from x402.extensions.payment_identifier import PAYMENT_IDENTIFIER
    from tests.test_x402_v2 import make_payload
    from x402.schemas import PaymentPayload
    cap = _cap()
    args = {"capability": cap}
    priv, did, cred = _bound_did()
    preq = payments.check_request(cap)
    d = make_payload(preq).model_dump(by_alias=True, exclude_none=True)
    d["extensions"] = {PAYMENT_IDENTIFIER: {
        "info": {"required": False, "id": "mcpreplay_" + uuid.uuid4().hex}}}
    payload = PaymentPayload(**d)
    env = _envelope(priv, did, "guild_check", args)
    meta = {MCP_PAYMENT_META_KEY: payload.model_dump(by_alias=True,
                                                     exclude_none=True),
            callerproof.MCP_META_KEY: env}
    r1 = _call("guild_check", args, meta=meta)
    assert not r1.is_error, r1.structured_content
    # replay: same identifier, same payload, same request — NO fresh proof
    r2 = _call("guild_check", args, meta={
        MCP_PAYMENT_META_KEY: payload.model_dump(by_alias=True,
                                                 exclude_none=True)})
    assert not r2.is_error
    assert r2.meta.get("x402/idempotent-replay") is True
    settle = r2.meta[MCP_PAYMENT_RESPONSE_META_KEY]
    assert settle.get("payer_attribution") == \
        "cryptographically_bound_machine_payer", (
        "the cached settlement record must preserve attribution fields")
    assert settle.get("caller_did") == did
