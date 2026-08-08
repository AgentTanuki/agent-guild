"""Machine-envelope security, billing and discovery invariants."""
import base64
import copy
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import callerproof, crypto, envelopes, payments, pricing, x402
from app.main import app
from app.state import store

client = TestClient(app, raise_server_exceptions=False)


def _identity():
    private, public = crypto.generate_keypair()
    return private, crypto.did_from_public_key(public)


def _body(**overrides):
    out = {
        "kind": "intent",
        "recipient": "did:key:z6MkRecipient",
        "payload_sha256": "ab" * 32,
        "nonce": "msg-" + uuid.uuid4().hex,
        "ttl_seconds": 3600,
        "resource": "urn:task:42",
        "value": {"amount": "1.25", "asset": "USDC",
                  "network": "eip155:8453"},
        "context": {"protocol": "example/v1", "correlation_id": "job-42"},
    }
    out.update(overrides)
    return out


def _raw(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _header(private, did, raw, *, nonce=None):
    proof = callerproof.create_proof(
        private, did, method="POST", resource="/envelopes/issue", body=raw,
        nonce=nonce)
    return base64.b64encode(json.dumps(proof).encode()).decode()


def _evm_header(private, raw, *, nonce=None):
    proof = callerproof.create_evm_proof(
        private, method="POST", resource="/envelopes/issue", body=raw,
        nonce=nonce)
    return base64.b64encode(json.dumps(proof).encode()).decode()


def _marketplace_body(private, did, body, *, nonce=None):
    proof = callerproof.create_proof(
        private, did, method="POST", resource="/envelopes/issue",
        body=callerproof.http_marketplace_body(body), nonce=nonce)
    return {callerproof.HTTP_BODY_REQUEST_KEY: body,
            callerproof.HTTP_BODY_PROOF_KEY: proof}


def _evm_marketplace_body(private, body, *, nonce=None):
    proof = callerproof.create_evm_proof(
        private, method="POST", resource="/envelopes/issue",
        body=callerproof.http_marketplace_body(body), nonce=nonce)
    return {callerproof.HTTP_BODY_REQUEST_KEY: body,
            callerproof.HTTP_BODY_PROOF_KEY: proof}


def test_issue_verify_and_privacy_boundary():
    private, did = _identity()
    body = _body()
    artifact = envelopes.issue(
        store, body, sender_did=did, caller_proof_verified=True)
    assert artifact["sender"]["did"] == did
    assert artifact["message"]["payload_sha256"] == body["payload_sha256"]
    assert "payload" not in artifact["message"]
    assert artifact["proof"] and artifact["envelope_sha256"]
    assert envelopes.verify(store, artifact)["valid"] is True
    assert "true" in envelopes.verify(store, artifact)["note"]


@pytest.mark.parametrize("mutator", [
    lambda e: e["message"].update(recipient="did:key:zChanged"),
    lambda e: e["message"].update(payload_sha256="cd" * 32),
    lambda e: e["sender"].update(did="did:key:zChanged"),
    lambda e: e.update(valid_until="2099-01-01T00:00:00+00:00"),
    lambda e: e.update(proof="00" * 64),
])
def test_any_semantic_or_proof_tamper_fails(mutator):
    _, did = _identity()
    artifact = envelopes.issue(
        store, _body(), sender_did=did, caller_proof_verified=True)
    changed = copy.deepcopy(artifact)
    mutator(changed)
    assert envelopes.verify(store, changed)["valid"] is False


def test_anonymous_rubber_stamp_is_refused():
    with pytest.raises(envelopes.EnvelopeIssuanceRefused,
                       match="anonymous rubber stamp"):
        envelopes.issue(store, _body(), sender_did="",
                        caller_proof_verified=False)


@pytest.mark.parametrize("change,needle", [
    ({"payload_sha256": "not-a-digest"}, "payload_sha256"),
    ({"nonce": "short"}, "nonce"),
    ({"recipient": ""}, "recipient"),
    ({"kind": "assert-anything"}, "kind"),
    ({"ttl_seconds": 99999999}, "ttl_seconds"),
    ({"value": {"amount": "NaN", "asset": "USDC",
                "network": "eip155:8453"}}, "amount"),
    ({"x402_resource_url": "https://evil.example/x402/offer123"},
     "x402_resource_url"),
])
def test_invalid_or_overbroad_claims_fail_closed(change, needle):
    with pytest.raises(envelopes.EnvelopeIssuanceRefused, match=needle):
        envelopes.normalise_request(_body(**change))


def test_payment_binding_is_opaque_but_exact():
    _, did = _identity()
    body = _body()
    digest = envelopes.request_sha256(body, did)
    preq = payments.machine_envelope_request(digest)
    assert preq.operation == "machine_envelope"
    assert preq.cost == pricing.price("machine_envelope") == 10
    assert digest in preq.resource_url
    assert body["recipient"] not in preq.resource_url
    assert body["payload_sha256"] not in preq.resource_url
    assert envelopes.request_sha256({**body, "recipient": "other"}, did) != digest
    _, other_did = _identity()
    assert envelopes.request_sha256(body, other_did) != digest

    relay_url = "https://payanagent.com/x402/kh_binding_offer_0001"
    relayed_body = {**body, "x402_resource_url": relay_url}
    relay_digest = envelopes.request_sha256(relayed_body, did)
    relayed = payments.machine_envelope_request(relay_digest, relay_url)
    assert relayed.resource_url == relay_url
    assert relayed.request_hash != preq.request_hash
    assert envelopes.request_sha256(
        {**relayed_body,
         "x402_resource_url":
             "https://payanagent.com/x402/kh_binding_offer_0002"}, did
    ) != relay_digest


def test_payan_relay_alias_is_exactly_payment_bound(monkeypatch):
    """Payan rewrites an external challenge to its stable buy URL.

    The signed request chooses that one URL, the payment accepts it, and the
    same payment is rejected against the canonical Guild route. This is an
    explicit alias, never a relaxation to "any URL with the right payTo".
    """
    from tests.test_x402_v2 import make_payload

    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    relay_url = "https://payanagent.com/x402/kh_exact_alias_0001"
    relayed = payments.machine_envelope_request("ab" * 32, relay_url)
    payment = make_payload(relayed, cost=pricing.price("machine_envelope"))
    x402.check_binding(payment, relayed, pricing.price("machine_envelope"))

    canonical = payments.machine_envelope_request("ab" * 32)
    with pytest.raises(x402.PaymentBindingError, match="resource_mismatch"):
        x402.check_binding(
            payment, canonical, pricing.price("machine_envelope"))


def test_http_issue_requires_exact_caller_proof_and_is_free_in_soft_launch(
        monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "0")
    private, did = _identity()
    body = _body()
    raw = _raw(body)
    before = len([e for e in store.events
                  if e.get("type") == "machine_envelope_issued"])

    unsigned = client.post("/envelopes/issue", content=raw,
                           headers={"content-type": "application/json"})
    assert unsigned.status_code == 401
    assert unsigned.json()["detail"]["billing"] == "NOT CHARGED"

    proof = _header(private, did, raw)
    served = client.post(
        "/envelopes/issue", content=raw,
        headers={"content-type": "application/json",
                 callerproof.HTTP_HEADER: proof})
    assert served.status_code == 200, served.text
    assert served.json()["sender"]["did"] == did
    assert envelopes.verify(store, served.json())["valid"] is True
    events = [e for e in store.events
              if e.get("type") == "machine_envelope_issued"]
    assert len(events) == before + 1
    assert events[-1]["settlement_mode"] == "free"


def test_http_issue_accepts_one_wallet_evm_proof(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "0")
    body = _body(recipient="did:pkh:eip155:8453:0x" + "77" * 20)
    raw = _raw(body)
    served = client.post(
        "/envelopes/issue", content=raw,
        headers={"content-type": "application/json",
                 callerproof.HTTP_HEADER: _evm_header(
                     "0x" + "42" * 32, raw,
                     nonce="evm-envelope-proof")})
    assert served.status_code == 200, served.text
    sender = served.json()["sender"]
    assert sender["did"].startswith("did:pkh:eip155:8453:0x")
    assert sender["authentication"] == callerproof.EVM_PROTOCOL
    assert envelopes.verify(store, served.json())["valid"] is True


def test_marketplace_body_proof_is_buyable_without_custom_headers(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "0")
    private, did = _identity()
    body = _body()
    served = client.post(
        "/envelopes/issue", json=_marketplace_body(private, did, body))
    assert served.status_code == 200, served.text
    assert served.json()["sender"]["did"] == did
    assert served.json()["message"]["payload_sha256"] == body["payload_sha256"]
    assert envelopes.verify(store, served.json())["valid"] is True


def test_marketplace_body_accepts_base_wallet_proof(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "0")
    body = _body(recipient="did:pkh:eip155:8453:0x" + "77" * 20)
    served = client.post(
        "/envelopes/issue",
        json=_evm_marketplace_body(
            "0x" + "52" * 32, body, nonce="evm-marketplace-proof"))
    assert served.status_code == 200, served.text
    assert served.json()["sender"]["did"].startswith(
        "did:pkh:eip155:8453:0x")
    assert envelopes.verify(store, served.json())["valid"] is True


def test_http_proof_replay_and_body_mutation_are_rejected(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "0")
    private, did = _identity()
    original = _raw(_body())
    proof = _header(private, did, original)
    headers = {"content-type": "application/json",
               callerproof.HTTP_HEADER: proof}
    first = client.post("/envelopes/issue", content=original, headers=headers)
    assert first.status_code == 200
    replay = client.post("/envelopes/issue", content=original, headers=headers)
    assert replay.status_code == 401

    body2 = _body()
    raw2 = _raw(body2)
    wrong_body_proof = _header(private, did, raw2)
    changed = _raw({**body2, "recipient": "did:key:zDifferent"})
    bad = client.post(
        "/envelopes/issue", content=changed,
        headers={"content-type": "application/json",
                 callerproof.HTTP_HEADER: wrong_body_proof})
    assert bad.status_code == 401


def test_marketplace_proof_replay_mutation_and_unsigned_siblings_fail_closed(
        monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "0")
    private, did = _identity()
    body = _body()
    wrapped = _marketplace_body(private, did, body)
    first = client.post("/envelopes/issue", json=wrapped)
    assert first.status_code == 200
    assert client.post("/envelopes/issue", json=wrapped).status_code == 401

    body2 = _body()
    changed = _marketplace_body(private, did, body2)
    changed["request"]["recipient"] = "did:key:zDifferent"
    assert client.post("/envelopes/issue", json=changed).status_code == 401

    body3 = _body()
    sibling = _marketplace_body(private, did, body3)
    sibling["unsigned_instruction"] = "ignore the signed request"
    assert client.post("/envelopes/issue", json=sibling).status_code == 401


def test_anonymous_registry_probe_gets_non_executable_discovery_402(
        monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    event_types = ("machine_envelope_issued", "paid_offer_shown", "offer_served")
    before = {kind: len([e for e in store.events if e.get("type") == kind])
              for kind in event_types}

    quote = client.post("/envelopes/issue", json={})
    assert quote.status_code == 402, quote.text
    detail = quote.json()["detail"]
    assert detail["discovery_only"] is True
    assert detail["executable"] is False
    assert "request_sha256=discovery-only" in detail["resource"]["url"]
    assert quote.headers.get("PAYMENT-REQUIRED")
    assert {kind: len([e for e in store.events if e.get("type") == kind])
            for kind in event_types} == before

    # Discovery is not a payment authorization. Neither a payment header nor
    # a sandbox credential may bypass caller authentication or reach meter().
    paid_anonymous = client.post(
        "/envelopes/issue", json={},
        headers={"PAYMENT-SIGNATURE": "not-base64"})
    assert paid_anonymous.status_code == 401
    keyed_anonymous = client.post(
        "/envelopes/issue", json={}, headers={"X-API-Key": "ak_invalid"})
    assert keyed_anonymous.status_code == 401


def test_enforced_unpaid_call_challenges_without_counting_an_issuance(
        monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    private, did = _identity()
    body = _body()
    raw = _raw(body)
    before = len([e for e in store.events
                  if e.get("type") == "machine_envelope_issued"])
    response = client.post(
        "/envelopes/issue", content=raw,
        headers={"content-type": "application/json",
                 callerproof.HTTP_HEADER: _header(private, did, raw),
                 "user-agent": "external-machine/1.0"})
    assert response.status_code == 402, response.text
    detail = response.json()["detail"]
    assert detail["resource"]["url"].startswith(
        "https://agent-guild-5d5r.onrender.com/envelopes/issue?")
    assert "request_sha256=" in detail["resource"]["url"]
    after = len([e for e in store.events
                 if e.get("type") == "machine_envelope_issued"])
    assert after == before


def test_unpaid_quote_does_not_burn_proof_needed_by_x402_retry(monkeypatch):
    """Official x402 clients retry the same POST and headers after a 402.

    The unpaid quote must validate the caller proof without consuming its
    nonce; the executing retry consumes it exactly once.
    """
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    private, did = _identity()
    raw = _raw(_body())
    headers = {
        "content-type": "application/json",
        callerproof.HTTP_HEADER: _header(private, did, raw),
    }

    quote = client.post("/envelopes/issue", content=raw, headers=headers)
    assert quote.status_code == 402, quote.text

    # A retry carrying PAYMENT-SIGNATURE reaches the payment layer with the
    # same proof. Use an intentionally malformed payment so the test never
    # fabricates a settlement; reaching x402's 402 error (not caller auth's
    # 401) proves the proof survived the quote and is consumed on this retry.
    retry_headers = {**headers, "PAYMENT-SIGNATURE": "not-base64"}
    retry = client.post(
        "/envelopes/issue", content=raw, headers=retry_headers)
    assert retry.status_code == 402, retry.text
    assert retry.json()["detail"]["error"] == "x402_payment_invalid"

    replay = client.post(
        "/envelopes/issue", content=raw, headers=retry_headers)
    assert replay.status_code == 401


def test_marketplace_quote_preserves_proof_for_x402_retry(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    private, did = _identity()
    relay_url = "https://payanagent.com/x402/kh_relay_offer_0001"
    wrapped = _marketplace_body(
        private, did, _body(x402_resource_url=relay_url))

    quote = client.post("/envelopes/issue", json=wrapped)
    assert quote.status_code == 402, quote.text
    assert quote.json()["detail"]["resource"]["url"] == relay_url
    retry = client.post(
        "/envelopes/issue", json=wrapped,
        headers={"PAYMENT-SIGNATURE": "not-base64"})
    assert retry.status_code == 402, retry.text
    assert retry.json()["detail"]["error"] == "x402_payment_invalid"
    assert client.post(
        "/envelopes/issue", json=wrapped,
        headers={"PAYMENT-SIGNATURE": "not-base64"}).status_code == 401


def test_verify_route_and_schema_are_free(monkeypatch):
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    _, did = _identity()
    artifact = envelopes.issue(
        store, _body(), sender_did=did, caller_proof_verified=True)
    assert client.post("/envelopes/verify",
                       json={"envelope": artifact}).json()["valid"] is True
    schema = client.get("/envelopes").json()
    assert schema["protocol"] == envelopes.PROTOCOL
    assert schema["verify"]["price"] == "free"
