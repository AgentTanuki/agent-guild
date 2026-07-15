"""Funnel semantics regressions (pre-mainnet swarm completion pass).

Four defects reproduced:

  * stages merged external, first-party and unknown traffic into one number;
  * a first-party mainnet CANARY settlement would have appeared in the same
    stage as external mainnet revenue;
  * the scout re-emitted `candidate_discovered` on every run — a refresh
    inflated discovery counts;
  * `supplier_contacted` was stamped BEFORE the send: a failed send read as
    a delivered contact.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.state import store
from app.swarm import scout

PAY_TO = "0x" + "11" * 20
EXT_UA = "external-agent-framework/1.4 (langgraph)"


@pytest.fixture(autouse=True)
def _enforced(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    monkeypatch.delenv("GUILD_SCOUT_CONTACT", raising=False)
    monkeypatch.delenv("GUILD_X402_FIRST_PARTY_PAYERS", raising=False)
    # settlement records inserted here must not leak into other modules'
    # honest-zero assertions — restore the billing log afterwards.
    billing_before = len(store.billing_log)
    yield
    del store.billing_log[billing_before:]


def _cap():
    return "funnel-sem-" + uuid.uuid4().hex[:8]


def _stage(funnel, name):
    return next(s for s in funnel["stages"] if s["stage"] == name)


def _counts(funnel):
    return {s["stage"]: s["count"] for s in funnel["stages"]}


# ---------------------------------------------------------------------------
# external / first-party / unknown separation
# ---------------------------------------------------------------------------

def test_demand_stage_separates_external_first_party_and_unknown():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        base = _stage(client.get("/funnel").json(), "demand_observed")
        b = base.get("breakdown", {})
        # external framework ask
        client.get(f"/check?capability={cap}-e",
                   headers={"User-Agent": EXT_UA})
        # first-party ask (our own probe, honestly tagged)
        client.get(f"/check?capability={cap}-f",
                   headers={"User-Agent": "guild-ops-check/1",
                            "X-Guild-Source": "ops"})
        # unknown/unattributable ask (bare tooling)
        client.get(f"/check?capability={cap}-u",
                   headers={"User-Agent": "curl/8.5.0"})
        st = _stage(client.get("/funnel").json(), "demand_observed")
        assert "breakdown" in st, "stages must separate attribution classes"
        d = st["breakdown"]
        assert d["external"] == b.get("external", 0) + 1
        assert d["first_party"] == b.get("first_party", 0) + 1
        assert d["unknown"] == b.get("unknown", 0) + 1
        # the headline count stays the honest external count
        assert st["count"] == d["external"]


# ---------------------------------------------------------------------------
# canary vs external mainnet settlement
# ---------------------------------------------------------------------------

def _mainnet_settlement(tx, first_party=None):
    rec = {"ok": True, "protocol": "v2", "network": "eip155:8453",
           "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
           "amount_atomic": "10000", "payer": "0x" + "22" * 20,
           "recipient": PAY_TO, "transaction": tx,
           "status": "settled_confirmed", "payment_identity": "pi-" + tx[-8:],
           "mainnet": True, "confirmed": True}
    if first_party is not None:
        rec["first_party_payer"] = first_party
    return rec


def test_canary_settlement_never_appears_as_external_revenue():
    from app.main import app
    with TestClient(app) as client:
        before = _counts(client.get("/funnel").json())
        store.record_x402_payment(
            "best_agent", 10,
            _mainnet_settlement("0x" + "c1" * 32, first_party=True))
        f = client.get("/funnel").json()
        c = _counts(f)
        assert "first_party_mainnet_canary" in c and \
            "external_mainnet_settlement" in c, (
                "the canary and external settlements must be separate stages")
        assert c["first_party_mainnet_canary"] == \
            before.get("first_party_mainnet_canary", 0) + 1
        assert c["external_mainnet_settlement"] == \
            before.get("external_mainnet_settlement", 0)
        src = _stage(f, "first_party_mainnet_canary")["source"].lower()
        assert "first-party" in src or "canary" in src
        assert "external" not in _stage(
            f, "first_party_mainnet_canary")["source"].split(" — ")[0].lower()


def test_external_settlement_counts_and_unflagged_is_unknown_never_external():
    from app.main import app
    with TestClient(app) as client:
        before = _counts(client.get("/funnel").json())
        store.record_x402_payment(
            "best_agent", 10,
            _mainnet_settlement("0x" + "c2" * 32, first_party=False))
        store.record_x402_payment(              # historical record, no flag
            "best_agent", 10, _mainnet_settlement("0x" + "c3" * 32))
        c = _counts(client.get("/funnel").json())
        assert c["external_mainnet_settlement"] == \
            before.get("external_mainnet_settlement", 0) + 1
        assert c["first_party_mainnet_canary"] == \
            before.get("first_party_mainnet_canary", 0)
        # the unattributable settlement lands in an explicit unknown bucket
        assert c.get("unknown_mainnet_settlement", 0) == \
            before.get("unknown_mainnet_settlement", 0) + 1


def test_known_canary_payer_addresses_reclassify_at_read_time(monkeypatch):
    """Historical canary settlements carry no flag; the operator can name the
    canary payer wallet(s) and they reclassify at READ time."""
    from app.main import app
    payer = "0x" + "ca" * 20
    rec = _mainnet_settlement("0x" + "c4" * 32)
    rec["payer"] = payer
    with TestClient(app) as client:
        store.record_x402_payment("best_agent", 10, rec)
        c1 = _counts(client.get("/funnel").json())
        monkeypatch.setenv("GUILD_X402_FIRST_PARTY_PAYERS", payer)
        c2 = _counts(client.get("/funnel").json())
        assert c2["first_party_mainnet_canary"] == \
            c1["first_party_mainnet_canary"] + 1
        assert c2["unknown_mainnet_settlement"] == \
            c1["unknown_mainnet_settlement"] - 1


def test_first_party_http_mainnet_payment_is_flagged_at_settle_time(
        monkeypatch):
    """A canary buying over HTTP with first-party headers must produce a
    settlement record flagged first-party at SETTLE time."""
    import base64 as b64mod
    from app import x402, x402_confirm
    from tests.test_x402_v2 import FakeFacilitator, make_payload, sig_header
    from tests.test_x402_cdp_settlement import (
        FAKE_KEY_ID, FAKE_SECRET, _receipt)
    from app import payments
    monkeypatch.setenv("GUILD_X402_NETWORK", "eip155:8453")
    monkeypatch.setenv("GUILD_X402_PAY_TO", x402.MAINNET_TREASURY)
    monkeypatch.setenv("CDP_API_KEY_ID", FAKE_KEY_ID)
    monkeypatch.setenv("CDP_API_KEY_SECRET", FAKE_SECRET)
    monkeypatch.setenv("GUILD_X402_CONFIRM_TIMEOUT", "0")
    monkeypatch.setenv("GUILD_FIRST_PARTY_TOKEN", "fp-secret")
    monkeypatch.setattr(x402, "_facilitator",
                        lambda: FakeFacilitator(network="eip155:8453"))
    monkeypatch.setattr(x402_confirm, "_get_receipt",
                        lambda tx, timeout=15.0: _receipt())
    from app.main import app
    cap = _cap()
    preq = payments.check_request(cap)
    with TestClient(app) as client:
        before = _counts(client.get("/funnel").json())
        r = client.get(
            f"/check?capability={cap}",
            headers={"User-Agent": "guild-canary/1",
                     "X-Agent-Guild-First-Party": "fp-secret",
                     "X-Agent-Guild-Role": "test",
                     "PAYMENT-SIGNATURE": sig_header(make_payload(preq))})
        assert r.status_code == 200
        rec = [b for b in store.billing_log
               if b.get("type") == "x402_payment"][-1]
        assert rec["mainnet"] is True and rec["confirmed"] is True
        assert rec.get("first_party_payer") is True
        c = _counts(client.get("/funnel").json())
        assert c["first_party_mainnet_canary"] == \
            before["first_party_mainnet_canary"] + 1
        assert c["external_mainnet_settlement"] == \
            before["external_mainnet_settlement"]


# ---------------------------------------------------------------------------
# candidate_discovered = first sight only; refresh is refresh
# ---------------------------------------------------------------------------

def _fixture_fetch(cap, endpoint):
    def fetch(url, **kw):
        if "a2aregistry.org" in url:
            return ({"agents": [{"name": "s-" + cap, "url": endpoint,
                                 "description": f"does {cap}"}]}, "ok")
        if url.startswith(endpoint):
            return ({"name": "s-" + cap, "url": endpoint,
                     "skills": [{"id": cap}]}, "ok")
        return ({"servers": [], "items": []}, "ok")
    return fetch


def test_rediscovery_emits_refreshed_not_discovered():
    from app.main import app
    cap = _cap()
    endpoint = f"https://{cap}.example"
    with TestClient(app) as client:
        client.get(f"/check?capability={cap}", headers={"User-Agent": EXT_UA})
        before = _counts(client.get("/funnel").json())
        probe = lambda u: {"reachable": True, "detail": "ok"}   # noqa: E731
        s1 = scout.run_scout(store, fetch=_fixture_fetch(cap, endpoint),
                             probe=probe)
        s2 = scout.run_scout(store, fetch=_fixture_fetch(cap, endpoint),
                             probe=probe)
        assert s1["discovered"] >= 1
        assert s2["discovered"] == 0, "a re-sighting is not a discovery"
        assert s2["refreshed"] >= 1
        after = _counts(client.get("/funnel").json())
        assert after["candidate_discovered"] == \
            before["candidate_discovered"] + 1
        assert after["candidate_refreshed"] >= \
            before.get("candidate_refreshed", 0) + 1


# ---------------------------------------------------------------------------
# contact_attempted vs contact_delivered
# ---------------------------------------------------------------------------

def test_failed_send_is_attempted_never_delivered(monkeypatch):
    from app.main import app
    monkeypatch.setenv("GUILD_SCOUT_CONTACT", "1")
    open_card = {"name": "x", "contact_policy": "open"}
    with TestClient(app) as client:
        before = _counts(client.get("/funnel").json())

        def send_fail(ep, msg):
            raise RuntimeError("connection refused")

        r = scout.maybe_contact(
            store, {"capability": _cap(),
                    "endpoint": "https://fail.example"},
            open_card, send_fail)
        assert r["contacted"] is True and r["delivered"] is False
        mid = _counts(client.get("/funnel").json())
        assert mid["contact_attempted"] == before.get("contact_attempted",
                                                      0) + 1
        assert mid["contact_delivered"] == before.get("contact_delivered", 0)

        def send_ok(ep, msg):
            return True

        r = scout.maybe_contact(
            store, {"capability": _cap(),
                    "endpoint": "https://ok.example"},
            open_card, send_ok)
        assert r["contacted"] is True and r["delivered"] is True
        after = _counts(client.get("/funnel").json())
        assert after["contact_attempted"] == mid["contact_attempted"] + 1
        assert after["contact_delivered"] == mid["contact_delivered"] + 1


# ---------------------------------------------------------------------------
# every source label describes exactly what the count contains
# ---------------------------------------------------------------------------

def test_source_labels_are_accurate():
    from app.main import app
    with TestClient(app) as client:
        f = client.get("/funnel").json()
        by = {s["stage"]: s["source"] for s in f["stages"]}
        assert "first sight" in by["candidate_discovered"].lower() or \
            "first" in by["candidate_discovered"].lower()
        assert "deliver" in by["contact_delivered"].lower()
        assert "attempt" in by["contact_attempted"].lower()
        ext = by["external_mainnet_settlement"].lower()
        assert "independently confirmed" in ext
        assert "first-party" in by["first_party_mainnet_canary"].lower() or \
            "canary" in by["first_party_mainnet_canary"].lower()
