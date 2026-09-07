"""Paid artifacts must work for an independent buyer, including after outages.

All payments are deterministic fakes; probes observe synthetic endpoints only.
"""
import copy
import base64
import hashlib
import importlib.util
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from app import abuse, crypto, deepcheck, main, payments, state, x402
from app.store import Store
from tests.test_x402_v2 import FakeFacilitator, PAY_TO, make_payload, sig_header
from tests.test_payment_identifier import _with_pid, _pid

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("buyer_evidence_sdk", ROOT / "sdk/agentguild_verify.py")
sdk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sdk)
URL = "https://Example.com:443/a2a/?task=private-observation"
AUDIENCE = "private:buyer/机器🦊"


@pytest.fixture(params=["json", "sqlite"])
def evidence_store(request, tmp_path, monkeypatch):
    monkeypatch.setenv("GUILD_STORE", request.param)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_X402_NETWORK", "eip155:84532")
    s = Store(path=str(tmp_path / "guild.json"))
    monkeypatch.setattr(state, "store", s)
    monkeypatch.setattr(main, "store", s)
    payments._inflight_reset_for_process_restart()
    x402.replay_guard._seen.clear()
    monkeypatch.setattr(deepcheck.preflight, "run", lambda *a, **kw: {
        "verdict": "no_failed_checks", "failed": [], "unknowns": [],
        "checks": [{"check": "endpoint_reachable", "status": "proven", "detail": "机器🦊"}]})
    return s


def issue(store):
    return deepcheck.evidence_bundle(store, URL, audience=AUDIENCE)


def verify(bundle, **kw):
    return sdk.verify_evidence_bundle(bundle, expected_issuer=bundle["issuer"],
                                     expected_endpoint=URL, expected_audience=AUDIENCE, **kw)


def resign(bundle, store):
    """Keep the outer signature valid so tests actually exercise inner proofs."""
    body = {k: v for k, v in bundle.items() if k not in ("proof", "bundle_sha256")}
    bundle["proof"] = crypto.sign_jcs(body, store.guild_identity()["private_key"])
    bundle["bundle_sha256"] = hashlib.sha256(sdk.evidence_canonical(
        {k: v for k, v in bundle.items() if k != "bundle_sha256"}).encode()).hexdigest()
    return bundle


def test_artifact_commits_exact_request_privately_and_survives_restart(evidence_store):
    s = evidence_store
    bundle = issue(s)
    assert bundle["requested_endpoint"] == URL
    assert bundle["subject_endpoint"] != URL
    out = verify(bundle)
    assert out["valid"] and out["ledger_inclusion_valid"] and out["checksum_valid"]
    body = {k: v for k, v in bundle.items() if k not in ("proof", "bundle_sha256")}
    assert crypto.verify_jcs(body, bundle["proof"], s.guild_identity()["public_key"])
    public = json.dumps(s.ledger_records, ensure_ascii=False)
    for private in (URL, AUDIENCE, "private-observation", bundle["commitment_nonce"]):
        assert private not in public
    restored = Store(path=s.path)
    record_id = bundle["ledger_anchor"]["inclusion"]["record"]["id"]
    assert restored.ledger_record(record_id)
    assert restored.durable_ledger().verify_chain()
    assert deepcheck.verify_bundle(restored, bundle)["valid"]


@pytest.mark.parametrize("prior_records", [0, 1, 2, 4])
def test_independent_python_and_node_accept_full_merkle_paths(evidence_store, prior_records):
    s = evidence_store
    for n in range(prior_records):
        s.append_ledger_event("config_change", {"test_sequence": n})
    bundle = issue(s)
    cases = [{"bundle": bundle, "options": {}, "valid": True}]
    assert verify(bundle)["valid"]
    for mutation in ("path", "record", "checkpoint", "feed", "digest", "missing", "checksum"):
        b = copy.deepcopy(bundle)
        a = b["ledger_anchor"]
        if mutation == "path":
            a["inclusion"]["path"].append({"position": "right", "hash": "0" * 64})
        elif mutation == "record":
            a["inclusion"]["record"]["body"]["snapshot_sha256"] = "0" * 64
        elif mutation == "checkpoint":
            a["checkpoint_entry"]["checkpoint"]["proof"] = "0" * 128
        elif mutation == "feed":
            a["checkpoint_entry"]["entry_proof"] = "0" * 128
        elif mutation == "digest":
            b["observation"]["verdict"] = "changed after commitment"
        elif mutation == "missing":
            del a["inclusion"]
        resign(b, s)
        if mutation == "checksum":
            b["bundle_sha256"] = "0" * 64
        out = verify(b)
        assert out["signature_valid"] and not out["valid"], (mutation, out)
        cases.append({"bundle": b, "options": {}, "valid": False})
    for options in ({"expectedEndpoint": URL + "different"},
                    {"expectedAudience": "different"}, {"expectedIssuer": "did:key:untrusted"}):
        cases.append({"bundle": bundle, "options": options, "valid": False})
    assert not sdk.verify_evidence_bundle(bundle, expected_issuer=bundle["issuer"],
                                         expected_endpoint=URL + "different")["valid"]
    assert not sdk.verify_evidence_bundle(bundle, expected_issuer=bundle["issuer"],
                                         expected_audience="different")["valid"]
    assert not sdk.verify_evidence_bundle(bundle, expected_issuer="did:key:untrusted")["valid"]
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for independent JavaScript verification")
    script = '''import {verifyEvidenceBundle} from "./sdk/agentguild_verify.mjs";
      import fs from "node:fs";
      const cases = JSON.parse(fs.readFileSync(0, "utf8"));
      console.log(JSON.stringify(cases.map(c => verifyEvidenceBundle(c.bundle,
        {expectedIssuer:c.bundle.issuer,expectedEndpoint:%s,expectedAudience:%s,...c.options}))));
    ''' % (json.dumps(URL), json.dumps(AUDIENCE))
    result = subprocess.run([node, "--input-type=module", "-e", script], input=json.dumps(cases),
                            text=True, capture_output=True, cwd=ROOT, check=True, timeout=15)
    assert [r["valid"] for r in json.loads(result.stdout)] == [c["valid"] for c in cases]


def test_expiry_and_malformed_objects_fail_closed(evidence_store):
    b = issue(evidence_store)
    out = verify(b, now=datetime.now(timezone.utc) + timedelta(days=8))
    assert not out["valid"] and out["signature_valid"] and out["expired"]
    for malformed in (None, [], {}, {**b, "ledger_anchor": None}, {**b, "valid_until": "bad"}):
        assert not sdk.verify_evidence_bundle(malformed, expected_issuer=b["issuer"])["valid"]


def test_unsupported_canonical_values_refuse_before_public_commitment(evidence_store, monkeypatch):
    monkeypatch.setattr(deepcheck, "deep_preflight", lambda *a: {"policy": {"score": 1e-7}})
    with pytest.raises(deepcheck.EvidenceIssuanceRefused):
        issue(evidence_store)
    assert not any(r.get("type") == "evidence_commitment" for r in evidence_store.ledger_records)


def test_ineligible_credentials_never_probe_commit_or_settle(evidence_store, monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("unfunded request reached artifact generation")
    monkeypatch.setattr(deepcheck, "evidence_bundle", forbidden)
    fac = FakeFacilitator(verify_ok=False)
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    preq = payments.evidence_bundle_request(URL, audience=AUDIENCE)
    account = evidence_store.create_account()
    evidence_store.charge(account["key"], account["balance"], "test_empty_account")
    body = {"url": URL, "audience": AUDIENCE}
    with TestClient(main.app) as client:
        for headers in ({}, {"X-API-Key": "unknown-key"}, {"X-API-Key": account["key"]},
                        {"PAYMENT-SIGNATURE": "malformed"},
                        {"PAYMENT-SIGNATURE": sig_header(make_payload(preq, cost=preq.cost))}):
            response = client.post("/evidence/bundle", json=body, headers=headers)
            assert response.status_code == 402, response.text
    assert not fac.settle_calls
    assert not any(r.get("type") == "evidence_commitment" for r in evidence_store.ledger_records)


def test_issuance_failure_does_not_charge_funded_buyer(evidence_store, monkeypatch):
    s = evidence_store
    account = s.create_account()
    s.credit(account["key"], 500, reason="synthetic test only")
    before = s.get_account(account["key"])["balance"]
    monkeypatch.setattr(s, "publish_checkpoint", lambda: None)
    with TestClient(main.app) as client:
        response = client.post("/evidence/bundle", json={"url": URL},
                               headers={"X-API-Key": account["key"]})
    assert response.status_code == 409
    assert s.get_account(account["key"])["balance"] == before


@pytest.mark.parametrize("mainnet_without_identifier", [False, True])
def test_paid_result_recovers_during_outage_without_new_issuance_or_payment(
        evidence_store, monkeypatch, mainnet_without_identifier):
    s = evidence_store
    if mainnet_without_identifier:
        from tests.test_x402_cdp_settlement import FAKE_KEY_ID, FAKE_SECRET, _receipt
        from tests.test_payment_recovery_no_identifier import _anchor
        monkeypatch.setenv("GUILD_X402_NETWORK", "eip155:8453")
        monkeypatch.setenv("GUILD_X402_PAY_TO", x402.MAINNET_TREASURY)
        monkeypatch.setenv("CDP_API_KEY_ID", FAKE_KEY_ID)
        monkeypatch.setenv("CDP_API_KEY_SECRET", FAKE_SECRET)
        _anchor(monkeypatch)
    fac = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    preq = payments.evidence_bundle_request(URL, audience=AUDIENCE)
    if mainnet_without_identifier:
        monkeypatch.setattr(x402.x402_confirm, "_get_receipt",
                            lambda tx, timeout=15.0: _receipt(
                                amount=int(x402.requirements(preq.cost).amount),
                                to=x402.MAINNET_TREASURY))
    payload = make_payload(preq, cost=preq.cost)
    if not mainnet_without_identifier:
        payload = _with_pid(payload, _pid())
    headers = {"PAYMENT-SIGNATURE": sig_header(payload)}
    body = {"url": URL, "audience": AUDIENCE}
    with TestClient(main.app) as client:
        quote = client.post("/evidence/bundle", json=body)
        required = json.loads(base64.b64decode(quote.headers["PAYMENT-REQUIRED"]))
        paid = client.post("/evidence/bundle", json=body, headers=headers)
        assert paid.status_code == 200, paid.text
        assert verify(paid.json())["valid"]
        Draft202012Validator(required["extensions"]["bazaar"]["info"]["output"]["schema"]).validate(paid.json())
        assert len(fac.settle_calls) == 1
        def unavailable(*a, **kw):
            raise AssertionError("completed purchase must not need fresh dependencies")
        monkeypatch.setattr(deepcheck, "evidence_bundle", unavailable)
        monkeypatch.setattr(x402, "_facilitator", unavailable)
        restored = Store(path=s.path)
        monkeypatch.setattr(state, "store", restored)
        monkeypatch.setattr(main, "store", restored)
        payments._inflight_reset_for_process_restart()
        monkeypatch.setenv("GUILD_ABUSE_CONTROLS", "1")
        monkeypatch.setenv("GUILD_RL_EVIDENCE_ISSUE", "0")
        original_binding = x402.check_binding
        def expired_payment(*a, **kw):
            raise x402.PaymentBindingError("payment_expired", "synthetic expired settlement window")
        monkeypatch.setattr(x402, "check_binding", expired_payment)
        retry = client.post("/evidence/bundle", json=body, headers=headers)
        assert retry.status_code == 200, retry.text
        assert retry.content == paid.content
        assert retry.headers.get("PAYMENT-RESPONSE")
        altered = copy.deepcopy(payload)
        altered.payload["signature"] = "0x" + "cd" * 65
        forged = client.post("/evidence/bundle", json=body,
                             headers={"PAYMENT-SIGNATURE": sig_header(altered)})
        assert forged.status_code == 409
        assert "payload_mismatch" in forged.text
        monkeypatch.setattr(x402, "check_binding", original_binding)
        different = client.post("/evidence/bundle", json={**body, "audience": "someone else"}, headers=headers)
        assert different.status_code == (402 if mainnet_without_identifier else 409)
    assert len(fac.settle_calls) == 1


def test_served_sdk_files_match_the_standalone_sources():
    for name in ("agentguild_verify.py", "agentguild_verify.mjs"):
        assert (ROOT / "sdk" / name).read_bytes() == (ROOT / "live/guild/app/artifacts" / name).read_bytes()


def test_new_issuance_burst_is_bounded_before_commitment(evidence_store, monkeypatch):
    monkeypatch.setenv("GUILD_ABUSE_CONTROLS", "1")
    monkeypatch.setenv("GUILD_RL_EVIDENCE_ISSUE", "1")
    abuse.reset()
    s = evidence_store
    account = s.create_account()
    s.credit(account["key"], 500, reason="synthetic test only")
    with TestClient(main.app) as client:
        args = {"json": {"url": URL}, "headers": {"X-API-Key": account["key"]}}
        assert client.post("/evidence/bundle", **args).status_code == 200
        before = len(s.ledger_records)
        balance = s.get_account(account["key"])["balance"]
        refused = client.post("/evidence/bundle", **args)
        assert refused.status_code == 429
        assert refused.json()["detail"]["bucket"] == "evidence_issue"
        assert len(s.ledger_records) == before
        assert s.get_account(account["key"])["balance"] == balance
    abuse.reset()
