"""x402 payment-identifier extension (official idempotency).

Requirements exercised:
  * identifiers persist across restarts (a fresh Store reloads them);
  * each id binds to payer + exact request hash + payload fingerprint +
    settlement + result hash;
  * same id + same request → the same cached result with NO second settlement;
  * reuse with a different payer, resource, parameters or payment fails closed;
  * concurrent duplicate requests: the in-flight one wins, the racer conflicts;
  * restart recovery: after a settle, a reload still serves the cached result.
"""
import base64
import json
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from x402.extensions.payment_identifier import PAYMENT_IDENTIFIER

from app import payments, x402
from tests.test_x402_v2 import FakeFacilitator, SEARCH, make_payload, sig_header

PAY_TO = "0x" + "11" * 20


@pytest.fixture(autouse=True)
def fac(monkeypatch, tmp_path):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    f = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: f)
    yield f


def _pid():
    return "pay_" + uuid.uuid4().hex


def _with_pid(payload, pid):
    d = payload.model_dump(by_alias=True, exclude_none=True)
    d["extensions"] = {PAYMENT_IDENTIFIER: {"info": {"required": False, "id": pid}}}
    from x402.schemas import PaymentPayload
    return PaymentPayload(**d)


def _hdr(payload):
    return {"PAYMENT-SIGNATURE": sig_header(payload)}


def test_challenge_declares_payment_identifier_extension(monkeypatch):
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/search?capability=x")
        challenge = json.loads(base64.b64decode(r.headers["PAYMENT-REQUIRED"]))
        assert PAYMENT_IDENTIFIER in challenge["extensions"]


def test_same_id_same_request_returns_cached_result_without_second_settlement(fac):
    from app.main import app
    pid = _pid()
    p = _with_pid(make_payload(SEARCH), pid)
    with TestClient(app) as client:
        r1 = client.get("/search?capability=anything", headers=_hdr(p))
        assert r1.status_code == 200
        first_body = r1.text
        assert len(fac.settle_calls) == 1
        # replay the SAME id + SAME request + SAME payload
        r2 = client.get("/search?capability=anything", headers=_hdr(p))
        assert r2.status_code == 200
        assert r2.headers.get("X-Guild-Payment-Idempotent-Replay") == "true"
        assert r2.text == first_body                   # byte-identical cache
        assert len(fac.settle_calls) == 1              # NO second settlement


def test_same_id_different_payload_conflicts():
    from app.main import app
    pid = _pid()
    with TestClient(app) as client:
        p1 = _with_pid(make_payload(SEARCH), pid)
        assert client.get("/search?capability=anything",
                          headers=_hdr(p1)).status_code == 200
        # same id, DIFFERENT payment payload (fresh nonce → different fingerprint)
        p2 = _with_pid(make_payload(SEARCH), pid)
        r = client.get("/search?capability=anything", headers=_hdr(p2))
        assert r.status_code == 409
        assert r.json()["reason"] == "payment_identifier_payload_mismatch"


def test_same_id_different_resource_conflicts():
    from app.main import app
    pid = _pid()
    with TestClient(app) as client:
        p1 = _with_pid(make_payload(SEARCH), pid)
        assert client.get("/search?capability=anything",
                          headers=_hdr(p1)).status_code == 200
        # reuse the id against a DIFFERENT resource (a different capability)
        other = payments.search_request("different-capability")
        p2 = _with_pid(make_payload(other), pid)
        r = client.get("/search?capability=different-capability",
                       headers=_hdr(p2))
        assert r.status_code == 409
        assert r.json()["reason"] in ("payment_identifier_resource_mismatch",
                                      "payment_identifier_payload_mismatch")


def test_same_id_different_payer_conflicts():
    from app.main import app
    pid = _pid()
    with TestClient(app) as client:
        p1 = _with_pid(make_payload(SEARCH), pid)
        assert client.get("/search?capability=anything",
                          headers=_hdr(p1)).status_code == 200
        # same id, a payload whose authorization.from is a different payer
        p2raw = make_payload(SEARCH)
        p2raw.payload["authorization"]["from"] = "0x" + "33" * 20
        p2 = _with_pid(p2raw, pid)
        r = client.get("/search?capability=anything", headers=_hdr(p2))
        assert r.status_code == 409
        assert r.json()["reason"] == "payment_identifier_payer_mismatch"


def test_identifier_persists_across_restart_and_serves_cached_result(tmp_path,
                                                                     monkeypatch):
    """Payment-identifier records survive a restart: a completed id + its
    cached result are reloaded by a FRESH Store from the same file, and a
    replay against the reloaded store raises CachedPaidResult (no
    re-settlement). Exercised at the store + gateway level so the shared app
    store is never swapped (which would pollute other suites)."""
    from app.store import Store
    data = str(tmp_path / "guild.json")
    s1 = Store(path=data)
    pid = _pid()
    payer = "0x" + "22" * 20
    preq = payments.search_request("anything")
    p = _with_pid(make_payload(preq), pid)
    fingerprint = payments._payload_fingerprint(p)
    s1.x402_payment_id_reserve(pid, payer=payer.lower(),
                               request_hash=preq.request_hash,
                               resource=preq.resource_url,
                               operation=preq.operation,
                               payload_fingerprint=fingerprint)
    s1.x402_payment_id_complete(pid, result_body='{"cached":true}',
                                result_sha256="abc", settle_header="hdr",
                                settle_extensions={}, settlement={"transaction": "0x1"})
    # RESTART: a brand-new Store from the same file reloads the record
    s2 = Store(path=data)
    rec = s2.x402_payment_id_get(pid)
    assert rec is not None and rec["status"] == "completed"
    assert rec["result_body"] == '{"cached":true}'
    # the gateway treats a matching replay as an idempotent cached result
    import app.state as state
    monkeypatch.setattr(state, "store", s2)
    with pytest.raises(payments.CachedPaidResult) as e:
        payments._handle_payment_identifier(p, preq)
    assert e.value.result_json == {"cached": True}


def test_concurrent_duplicate_requests_settle_once(fac):
    """Two threads fire the SAME id + request simultaneously: exactly one
    settlement happens; the other either gets the cached result or an
    in-flight conflict — never a second settlement."""
    from app.main import app
    pid = _pid()
    p = _with_pid(make_payload(SEARCH), pid)
    results = []
    with TestClient(app) as client:
        def _fire():
            results.append(client.get("/search?capability=anything",
                                      headers=_hdr(p)).status_code)
        threads = [threading.Thread(target=_fire) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len(fac.settle_calls) == 1                  # settled exactly once
    assert 200 in results                              # at least one served
    # the racer is either a cached 200 or a 409 in-flight conflict
    assert all(code in (200, 409) for code in results)
