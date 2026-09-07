"""Payment observations explain failures without changing payment safety."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from app import a2a_x402, main, mcp_server, paymentdiag, payments, state, x402, x402_confirm
from app import store as store_module
from app.store import Store
from tests.test_payment_identifier import _with_pid, _pid
from tests.test_x402_v2 import FakeFacilitator, SEARCH, make_payload, sig_header, PAY_TO


@pytest.fixture(params=["json", "sqlite"])
def isolated(request, tmp_path, monkeypatch):
    monkeypatch.setenv("GUILD_STORE", request.param)
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "guild.sqlite3"))
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_X402_NETWORK", "eip155:84532")
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.setenv("GUILD_X402_RECOVERY_LEASE_S", "0")
    s = Store(path=str(tmp_path / "guild.json"))
    for module in (state, main, mcp_server, a2a_x402):
        monkeypatch.setattr(module, "store", s)
    payments._inflight_reset_for_process_restart()
    x402.replay_guard._seen.clear()
    yield s
    payments._inflight_reset_for_process_restart()
    x402.replay_guard._seen.clear()


def observations(s):
    return list(s.measurement_event_view(types=("payment_stage_observed",))[0])


def stages(s):
    return {e["stage"] for e in observations(s)}


def test_http_malformed_is_present_not_absent_and_redacts_input(isolated):
    secret = "never-record-this-payment-signature"
    response = TestClient(main.app).get("/search?capability=anything", headers={"PAYMENT-SIGNATURE": secret})
    assert response.status_code == 402
    events = observations(isolated)
    assert stages(isolated) == {"credential_present", "rejected"}
    assert events[-1]["reason_code"] == "malformed_credential"
    assert len({e["attempt_id"] for e in events}) == 1
    assert secret not in json.dumps(events)
    assert all(e["key"] == "anon" and e["ua"] == "" for e in events)


def test_missing_credential_does_not_imply_an_attempt_or_buyer(isolated):
    with pytest.raises(payments.PaymentChallenge):
        payments.authorize(SEARCH)
    assert observations(isolated) == []
    report = paymentdiag.summary(isolated)
    assert "not unique buyers" in report["measure"]
    assert "no wallet, no budget" in report["limitations"]
    assert report["rows"] == []


def test_binding_failure_never_contacts_facilitator(isolated, monkeypatch):
    fac = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    with pytest.raises(x402.PaymentBindingError):
        payments.authorize(SEARCH, payment=make_payload(url="https://private.example/secret"))
    assert not fac.verify_calls and not fac.settle_calls
    assert "binding_valid" not in stages(isolated)
    assert observations(isolated)[-1]["reason_code"] == "resource_mismatch"
    assert "private.example" not in json.dumps(observations(isolated))


@pytest.mark.parametrize("failure,reason,last", [
    ("verify_rejected", "facilitator_verify_rejected", "facilitator_verify_started"),
    ("verify_error", "facilitator_verify_error", "facilitator_verify_started"),
    ("settle_rejected", "facilitator_settle_rejected", "facilitator_settle_started"),
    ("settle_error", "facilitator_settle_error", "facilitator_settle_started"),
])
def test_facilitator_boundaries_and_opaque_errors(isolated, monkeypatch, failure, reason, last):
    secret = "provider-error-containing-a-signature"
    fac = FakeFacilitator(verify_ok=failure != "verify_rejected", settle_ok=failure != "settle_rejected",
                          raise_error=RuntimeError(secret) if failure == "verify_error" else None)
    if failure == "settle_error":
        def fail(*args):
            raise RuntimeError(secret)
        fac.settle = fail
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    with pytest.raises(payments.PaymentChallenge):
        payments.authorize(SEARCH, payment=make_payload())
    assert last in stages(isolated)
    assert "authorization_accepted" not in stages(isolated)
    assert reason in {e["reason_code"] for e in observations(isolated)}
    assert secret not in json.dumps(observations(isolated))


def test_mainnet_confirmation_recovery_never_counts_a_second_settlement(isolated, monkeypatch):
    monkeypatch.setenv("GUILD_X402_NETWORK", "eip155:8453")
    monkeypatch.setattr(x402, "config_errors", lambda: [])
    monkeypatch.setattr(x402_confirm, "current_block", lambda *args: 1000)
    confirmed = False
    monkeypatch.setattr(x402_confirm, "confirm_settlement", lambda *a, **k: {"confirmed": confirmed})
    fac = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    payload = _with_pid(make_payload(), _pid())
    with pytest.raises(payments.PaymentChallenge):
        payments.authorize(SEARCH, payment=payload)
    assert "facilitator_settlement_accepted" in stages(isolated)
    assert "chain_confirmed" not in stages(isolated)
    assert "authorization_accepted" not in stages(isolated)
    first_id = observations(isolated)[0]["attempt_id"]
    confirmed = True
    auth = payments.authorize(SEARCH, payment=payload)
    auth.settled.finalize(b'{"result":"synthetic"}')
    events = [e for e in observations(isolated) if e["attempt_id"] != first_id]
    assert len(fac.settle_calls) == 1
    assert {e["stage"] for e in events} >= {"recovery_started", "chain_confirmed", "result_prepared"}
    assert "facilitator_settle_started" not in {e["stage"] for e in events}
    assert len({e["attempt_id"] for e in events}) == 1


def test_http_prepared_result_and_cached_replay_have_separate_invocations(isolated, monkeypatch):
    fac = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    payload = _with_pid(make_payload(), _pid())
    client = TestClient(main.app)
    headers = {"PAYMENT-SIGNATURE": sig_header(payload)}
    a = client.get("/search?capability=anything", headers=headers)
    b = client.get("/search?capability=anything", headers=headers)
    assert a.status_code == b.status_code == 200 and a.content == b.content
    assert len(fac.settle_calls) == 1
    assert stages(isolated) >= {"result_prepared", "cached_result_prepared"}
    assert "chain_confirmed" not in stages(isolated)
    assert len({e["attempt_id"] for e in observations(isolated)}) == 2
    assert paymentdiag.current() is None


def test_mcp_bad_metadata_is_observed_even_when_legacy_parser_ignores_it(isolated):
    ctx = SimpleNamespace(request_context=SimpleNamespace(meta=SimpleNamespace(
        model_extra={"x402/payment": {"secret": "opaque-secret"}})))
    result = mcp_server._serve_paid(SEARCH, lambda: pytest.fail("unpaid result"), ctx)
    assert result is not None
    assert "credential_present" in stages(isolated)
    assert "credential_absent" not in stages(isolated)
    assert "opaque-secret" not in json.dumps(observations(isolated))


def test_a2a_operation_scope_and_malformed_payment(isolated):
    preq = payments.check_request("anything")
    task = a2a_x402.build_payment_required_task(preq, preq.cost)
    a2a_x402.handle_payment_submission({"taskId": task["id"], "metadata": {
        a2a_x402.PAYLOAD_KEY: "never-record-payload"}})
    report = paymentdiag.summary(isolated, "best_agent")
    assert {r["stage"] for r in report["rows"]} == {"payment_submission_present", "credential_present", "rejected"}
    assert all(r["transport"] == "a2a" for r in report["rows"])
    assert not paymentdiag.summary(isolated, "signed_decision")["rows"]


def test_telemetry_failure_cannot_fail_a_successful_payment(isolated, monkeypatch):
    real_record = isolated.record_event
    def fail_diagnostic(key, etype, *args, **kwargs):
        if etype == "payment_stage_observed":
            raise OSError("synthetic-disk-error")
        return real_record(key, etype, *args, **kwargs)
    monkeypatch.setattr(isolated, "record_event", fail_diagnostic)
    fac = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    auth = payments.authorize(SEARCH, payment=make_payload())
    assert auth.mode == "x402" and len(fac.settle_calls) == 1
    auth.settled.finalize(b'{}')


def test_concurrent_contexts_and_reason_codes_cannot_leak_or_grow(isolated):
    barrier = threading.Barrier(2)
    def run(operation):
        with paymentdiag.observe(operation, "http", operation == "best_agent"):
            paymentdiag.emit("credential_present")
            barrier.wait(timeout=5)
            paymentdiag.reject({"signature": "do-not-log-secret"})
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(run, ["best_agent", "signed_decision"]))
    ids = {}
    for event in observations(isolated):
        ids.setdefault(event["attempt_id"], set()).add(event["operation"])
        assert event["reason_code"] in paymentdiag.REASONS
    assert sorted(map(sorted, ids.values())) == [["best_agent"], ["signed_decision"]]
    assert "do-not-log-secret" not in json.dumps(observations(isolated))
    assert paymentdiag.current() is None


def test_durable_stage_and_passport_history_survive_cache_trim(isolated, monkeypatch):
    monkeypatch.setattr(store_module, "EVENT_RETENTION_TRIGGER", 5)
    monkeypatch.setattr(store_module, "EVENT_RETENTION_TARGET", 3)
    with paymentdiag.observe("best_agent", "http"):
        paymentdiag.emit("credential_present")
    isolated.record_event(None, "passport_requested")
    isolated.record_event(None, "passport_issued", subject_id="subject")
    isolated.record_event(None, "passport_issue_failed", reason="unknown_agent_or_no_reputation")
    for _ in range(10):
        isolated.record_event(None, "filler")
    report = paymentdiag.summary(isolated)
    passport = isolated.passport_activity()
    assert passport["measurement_version"] == "passport-activity-v2"
    if isolated.backend is not None:
        assert len(report["rows"]) == 1
        assert passport["event_counts"]["passport_issued_events"] == 1
        assert passport["issuer_failure_reasons"] == {"unknown_agent_or_no_reputation": 1}
        assert passport["measurement_coverage"]["history_complete"] is True
    else:
        assert report["measurement_coverage"]["history_complete"] is False
        assert passport["measurement_coverage"]["history_complete"] is False
