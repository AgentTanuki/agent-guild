"""A2A caller-proof parity (agent-guild/caller-proof/v1, A2A mapping).

The public schema (/caller-proof) has advertised the A2A transport mapping
since 81f2fa4, but the real JSON-RPC message/send path never read or
verified it. These tests drive the ACTUAL /a2a endpoint and prove:

  * the envelope in message.metadata["io.agent-guild/caller-proof"] is
    verified exactly once per request, bound to method="message/send",
    resource="/a2a" and body = JCS of the message PARTS only (the proof is
    never circularly signed);
  * a valid proof records VERIFIED machine demand under actor did:<did>;
  * one request's single verification serves BOTH demand and paid x402
    settlement attribution — the nonce is never verified twice;
  * body / DID / resource tampering and replay all fail;
  * anonymous A2A stays fully supported, merely UNVERIFIED;
  * known first-party DIDs remain first-party;
  * a bound payer becomes cryptographically_bound_machine_payer — NEVER
    independently external without a separate attestation.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import a2a_x402, callerproof, crypto, payments, walletbinding, \
    x402, x402_confirm
from app.state import store

MAINNET = "eip155:8453"
PAYER = "0x" + "22" * 20


@pytest.fixture(autouse=True)
def _clean_residue():
    events_before = len(store.events)
    dedupe_before = dict(store.demand_dedupe)
    yield
    del store.events[events_before:]
    store.demand_dedupe.clear()
    store.demand_dedupe.update(dedupe_before)


def _cap():
    return "a2acp-" + uuid.uuid4().hex[:8]


def _did():
    priv, pub = crypto.generate_keypair()
    return priv, crypto.did_from_public_key(pub)


def _parts(text):
    return [{"kind": "text", "text": text}]


def _envelope(priv, did, parts, *, resource="/a2a",
              method="message/send"):
    return callerproof.create_proof(
        priv, did, method=method, resource=resource,
        body=callerproof.a2a_parts_body(parts))


def _send(client, parts, metadata=None, headers=None):
    message = {"role": "user", "parts": parts}
    if metadata:
        message["metadata"] = metadata
    return client.post("/a2a", json={
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": message}}, headers=headers or {})


def _demand_event(cap):
    evs = [e for e in store.events
           if e.get("type") == "capability_demand"
           and e.get("capability") == cap]
    assert evs, f"no capability_demand event for {cap}"
    return evs[-1]


# ---------------------------------------------------------------------------
# soft launch: demand attribution
# ---------------------------------------------------------------------------

def test_valid_proof_reaches_verified_demand():
    from app.main import app
    cap = _cap()
    priv, did = _did()
    parts = _parts(f"check: {cap}")
    env = _envelope(priv, did, parts)
    with TestClient(app) as client:
        r = _send(client, parts, {callerproof.A2A_METADATA_KEY: env})
        assert r.status_code == 200 and "result" in r.json()
    ev = _demand_event(cap)
    assert ev["caller_proof_verified"] is True, (
        "a valid A2A caller proof must record VERIFIED machine demand — "
        "the metadata envelope was not verified on the message/send path")
    assert ev["caller_did"] == did
    assert ev["actor"] == "did:" + did


def test_anonymous_a2a_stays_supported_but_unverified():
    from app.main import app
    cap = _cap()
    parts = _parts(f"check: {cap}")
    with TestClient(app) as client:
        r = _send(client, parts)
        assert r.status_code == 200 and "result" in r.json()
    ev = _demand_event(cap)
    assert ev["caller_proof_verified"] is False
    assert not ev.get("caller_did")
    assert ev["actor"].startswith("a2a:")


def test_body_tampering_fails_the_binding():
    from app.main import app
    cap = _cap()
    priv, did = _did()
    # signed over DIFFERENT parts than the ones actually sent
    env = _envelope(priv, did, _parts("check: something-else"))
    parts = _parts(f"check: {cap}")
    with TestClient(app) as client:
        r = _send(client, parts, {callerproof.A2A_METADATA_KEY: env})
        assert r.status_code == 200
    ev = _demand_event(cap)
    assert ev["caller_proof_verified"] is False


def test_did_tampering_fails_signature():
    from app.main import app
    cap = _cap()
    priv, did = _did()
    _, other_did = _did()
    parts = _parts(f"check: {cap}")
    env = _envelope(priv, did, parts)
    env["payload"]["did"] = other_did          # forged after signing
    with TestClient(app) as client:
        _send(client, parts, {callerproof.A2A_METADATA_KEY: env})
    assert _demand_event(cap)["caller_proof_verified"] is False


def test_resource_tampering_fails_binding():
    from app.main import app
    cap = _cap()
    priv, did = _did()
    parts = _parts(f"check: {cap}")
    env = _envelope(priv, did, parts, resource="/other")
    with TestClient(app) as client:
        _send(client, parts, {callerproof.A2A_METADATA_KEY: env})
    assert _demand_event(cap)["caller_proof_verified"] is False


def test_replay_fails_the_second_time():
    from app.main import app
    cap = _cap()
    priv, did = _did()
    parts = _parts(f"check: {cap}")
    env = _envelope(priv, did, parts)
    with TestClient(app) as client:
        _send(client, parts, {callerproof.A2A_METADATA_KEY: env})
        assert _demand_event(cap)["caller_proof_verified"] is True
        _send(client, parts, {callerproof.A2A_METADATA_KEY: env})
    ev2 = _demand_event(cap)
    assert ev2["caller_proof_verified"] is False, (
        "a replayed envelope must never verify again (durable nonce)")


def test_metadata_is_excluded_from_the_signed_body():
    """The proof binds the PARTS only — adding unrelated metadata (or the
    proof itself) must not invalidate it, or the mapping would be circular
    and unimplementable."""
    from app.main import app
    cap = _cap()
    priv, did = _did()
    parts = _parts(f"check: {cap}")
    env = _envelope(priv, did, parts)
    with TestClient(app) as client:
        _send(client, parts, {callerproof.A2A_METADATA_KEY: env,
                              "some.other/metadata": {"x": 1}})
    assert _demand_event(cap)["caller_proof_verified"] is True


def test_known_first_party_did_remains_first_party():
    from app.main import app
    cap = _cap()
    priv, pub = crypto.generate_keypair()
    agent = store.register_agent(name="a2a-fp-agent", capabilities=["ops"],
                                 metadata={}, public_key=pub,
                                 first_party=True)
    did = agent["did"]
    parts = _parts(f"check: {cap}")
    env = _envelope(priv, did, parts)
    with TestClient(app) as client:
        _send(client, parts, {callerproof.A2A_METADATA_KEY: env})
    ev = _demand_event(cap)
    assert ev["demand_first_party"] is True, (
        "a KNOWN first-party DID must stay first-party however valid its "
        "proof")


# ---------------------------------------------------------------------------
# enforced + paid: settlement attribution through the REAL A2A x402 flow
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


def _bound_did():
    priv, pub = crypto.generate_keypair()
    did = crypto.did_from_public_key(pub)
    cred = walletbinding.issue_credential(store, did=did, address=PAYER,
                                          network=MAINNET,
                                          challenge_nonce="a-" +
                                          uuid.uuid4().hex)
    return priv, did, cred


def _last_payment():
    return [b for b in store.billing_log
            if b.get("type") == "x402_payment"][-1]


def _submit_payment(client, task_id, priv, did, cap):
    """A payment-submitted message/send carrying the x402 payload AND a
    fresh caller proof over ITS OWN (empty) parts."""
    from tests.test_x402_v2 import make_payload
    payload = make_payload(payments.check_request(
        cap)).model_dump(by_alias=True, exclude_none=True)
    parts: list = []
    metadata = {
        a2a_x402.STATUS_KEY: "payment-submitted",
        a2a_x402.PAYLOAD_KEY: payload,
    }
    if priv is not None:
        metadata[callerproof.A2A_METADATA_KEY] = _envelope(priv, did, parts)
    message = {"role": "user", "parts": parts, "taskId": task_id,
               "metadata": metadata}
    return client.post("/a2a", json={
        "jsonrpc": "2.0", "id": 2, "method": "message/send",
        "params": {"message": message}})


def test_one_request_one_proof_serves_demand_then_settlement(_paid_mainnet):
    """The full paid A2A flow with a proof on EACH request: the ask's proof
    creates verified demand; the submission's proof attributes the
    settlement. Each envelope is verified exactly once — if the endpoint
    verified twice within a request, the nonce replay would leave the
    settlement unverified."""
    from app.main import app
    cap = _cap()
    priv, did, cred = _bound_did()
    ask_parts = _parts(f"check: {cap}")
    with TestClient(app) as client:
        # 1. capability ask with proof → payment-required task + verified
        #    demand recorded pre-authorization
        r = _send(client, ask_parts,
                  {callerproof.A2A_METADATA_KEY:
                   _envelope(priv, did, ask_parts)})
        task = r.json()["result"]
        assert task["kind"] == "task"
        assert task["status"]["message"]["metadata"][
            a2a_x402.STATUS_KEY] == "payment-required"
        ev = _demand_event(cap)
        assert ev["caller_proof_verified"] is True
        assert ev["actor"] == "did:" + did
        # 2. payment submission with ITS OWN proof → settlement attributed
        r2 = _submit_payment(client, task["id"], priv, did, cap)
        result = r2.json()["result"]
        assert result["status"]["state"] == "completed", result
    rec = _last_payment()
    assert rec["payer_attribution"] == \
        "cryptographically_bound_machine_payer", (
        "a valid caller proof + exact wallet binding on the A2A settlement "
        "path must attribute the payer as cryptographically bound")
    assert rec["caller_did"] == did
    assert rec["wallet_binding_credential"] == cred["credential_id"]


def test_bound_payer_never_external_without_attestation(_paid_mainnet,
                                                        monkeypatch):
    monkeypatch.delenv("GUILD_EXTERNALITY_ATTESTOR_DIDS", raising=False)
    from app.main import app
    cap = _cap()
    priv, did, cred = _bound_did()
    with TestClient(app) as client:
        r = _send(client, _parts(f"check: {cap}"))
        task = r.json()["result"]
        r2 = _submit_payment(client, task["id"], priv, did, cap)
        assert r2.json()["result"]["status"]["state"] == "completed"
        rev = client.get("/billing/revenue").json()["real_settlement"]
    rec = _last_payment()
    assert rec["payer_attribution"] == \
        "cryptographically_bound_machine_payer"
    assert rec["first_party_payer"] is None
    assert rev["attribution"]["independently_attested_external_machine"][
        "transactions"] == 0
    assert rev["independently_attested_external_revenue_usd"] == 0.0


def test_unproven_a2a_settlement_stays_unverified(_paid_mainnet):
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        r = _send(client, _parts(f"check: {cap}"))
        task = r.json()["result"]
        r2 = _submit_payment(client, task["id"], None, "", cap)
        assert r2.json()["result"]["status"]["state"] == "completed"
    rec = _last_payment()
    assert rec["payer_attribution"] == "unverified_payer"
    assert rec.get("caller_did") in (None, "")
