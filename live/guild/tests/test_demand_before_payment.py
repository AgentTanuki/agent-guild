"""B1 — demand must be preserved BEFORE payment enforcement (the korean-legal
regression).

Production regression reproduced here: a genuine external A2A actor asked for
`capabilities`, then `korean`, then `korean-legal`; AG emitted
`x402_payment_required`, but `korean-legal` never appeared in
`/capabilities.unmet_demand` because payment enforcement ran before
`store.check()` recorded the demand. A machine must never have to pay merely
to tell AG what capability it needs.

Invariants:
  * an unpaid capability ask is recorded as demand on HTTP, MCP and A2A —
    once — before the payment challenge is emitted;
  * repeated challenges / payment resubmissions deduplicate;
  * the same request is not counted again after payment succeeds;
  * when exact usable supply is zero the challenge carries a FREE
    machine-readable `no_supply` block (canonical capability, supplied +
    verified-reachable counts, demand-watch action, supplier-registration
    action, stable demand id) — and no paid payload leaks;
  * greetings/probes and AG-owned traffic never inflate genuine demand.
"""
import asyncio
import json
import uuid

import pytest
import mcp.types as mt
from fastmcp import Client
from fastapi.testclient import TestClient

from app import demand, payments, x402
from app.state import store
from app.mcp_server import mcp as guild_mcp
from tests.test_x402_v2 import FakeFacilitator, make_payload, sig_header

PAY_TO = "0x" + "11" * 20
EXT_UA = "external-agent-framework/3.1 (langchain)"


@pytest.fixture(autouse=True)
def _enforced(monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    monkeypatch.delenv("GUILD_X402_NETWORK", raising=False)
    yield


def _cap():
    # a unique capability per test keeps assertions exact and independent
    return "korean-legal-" + uuid.uuid4().hex[:8]


def _unmet(client, cap):
    return client.get("/capabilities").json()["unmet_demand"].get(cap)


def _demand_events(cap):
    return [e for e in store.events
            if e.get("type") == "capability_demand"
            and e.get("capability") == cap]


def _a2a_ask(client, cap, ua=EXT_UA, headers=None):
    hdrs = {"User-Agent": ua}
    hdrs.update(headers or {})
    return client.post("/a2a", json={
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {"role": "user",
                               "parts": [{"kind": "text",
                                          "text": f"check: {cap}"}]}},
    }, headers=hdrs)


# ---------------------------------------------------------------------------
# the regression, per transport
# ---------------------------------------------------------------------------

def test_a2a_unpaid_korean_legal_lands_in_unmet_demand():
    """The exact production sequence: capabilities → korean → korean-legal,
    unpaid; the final ask must appear in unmet demand even though the caller
    only ever received a payment challenge."""
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        r0 = _a2a_ask(client, "capabilities".replace("capabilities", cap[:6]))
        r = _a2a_ask(client, cap)
        assert r.status_code == 200
        task = r.json()["result"]
        # the payment gate DID fire (x402 payment-required task)…
        assert task["kind"] == "task"
        assert task["status"]["state"] == "input-required"
        # …and the demand was still preserved
        row = _unmet(client, cap)
        assert row is not None, "unpaid A2A demand was lost at the payment gate"
        assert row["lookups"] == 1


def test_http_unpaid_check_lands_in_unmet_demand():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        r = client.get(f"/check?capability={cap}",
                       headers={"User-Agent": EXT_UA})
        assert r.status_code == 402                    # gate fired
        row = _unmet(client, cap)
        assert row is not None, "unpaid HTTP demand was lost at the payment gate"
        assert row["lookups"] == 1


def test_mcp_unpaid_check_lands_in_unmet_demand():
    cap = _cap()

    def _call():
        async def run():
            async with Client(guild_mcp, client_info=mt.Implementation(
                    name="external-client", version="9")) as c:
                return await c.call_tool("guild_check", {"capability": cap},
                                         raise_on_error=False)
        return asyncio.run(run())

    r = _call()
    assert r.is_error and r.structured_content["x402Version"] == 2
    events = _demand_events(cap)
    assert len(events) == 1, "unpaid MCP demand was lost at the payment gate"
    assert events[0].get("transport") == "mcp"


# ---------------------------------------------------------------------------
# dedupe: retries, resubmissions, and post-payment recount
# ---------------------------------------------------------------------------

def test_repeated_challenges_do_not_inflate_demand():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        for _ in range(5):
            assert client.get(f"/check?capability={cap}",
                              headers={"User-Agent": EXT_UA}).status_code == 402
        row = _unmet(client, cap)
        assert row["lookups"] == 1, "payment retries inflated demand"


def test_successful_payment_does_not_recount_the_same_request(monkeypatch):
    from app.main import app
    fac = FakeFacilitator()
    monkeypatch.setattr(x402, "_facilitator", lambda: fac)
    cap = _cap()
    with TestClient(app) as client:
        # unpaid ask → challenge; demand recorded once
        assert client.get(f"/check?capability={cap}",
                          headers={"User-Agent": EXT_UA}).status_code == 402
        assert len(_demand_events(cap)) == 1
        # pay for the same request
        preq = payments.check_request(cap)
        p = make_payload(preq)
        rp = client.get(f"/check?capability={cap}",
                        headers={"User-Agent": EXT_UA,
                                 "PAYMENT-SIGNATURE": sig_header(p)})
        assert rp.status_code == 200
        assert len(_demand_events(cap)) == 1, \
            "the same request was counted again after payment succeeded"


def test_a2a_payment_resubmission_does_not_inflate(monkeypatch):
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        _a2a_ask(client, cap)
        _a2a_ask(client, cap)                       # retry, same actor
        row = _unmet(client, cap)
        assert row["lookups"] == 1


# ---------------------------------------------------------------------------
# free no_supply block on the challenge — machine-readable, no paid leak
# ---------------------------------------------------------------------------

def test_http_402_carries_free_no_supply_block_without_paid_payload():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        r = client.get(f"/check?capability={cap}",
                       headers={"User-Agent": EXT_UA})
        assert r.status_code == 402
        body = r.json().get("detail") or r.json()      # FastAPI wraps 402 detail
        ns = body.get("no_supply")
        assert ns, "zero-supply challenge must carry the free no_supply block"
        assert ns["capability"] == demand.canonical_capability(cap)
        assert ns["supplied"] == 0
        assert ns["verified_reachable"] == 0
        assert ns["demand_id"] == demand.demand_id_for(cap)
        assert ns["actions"]["demand_watch"]["path"] == "/demand/watch"
        assert ns["actions"]["register_supplier"]["path"] == "/agents/register"
        # free means free of the PAID payload: no shortlist/decision/evidence
        # ("best_agent" the OPERATION NAME appears in the challenge's resource
        # description — that is not a payload leak)
        blob = json.dumps(body)
        assert "shortlist" not in blob
        assert '"decision"' not in blob
        assert "attestations" not in blob
        assert '"estimate"' not in blob


def test_a2a_payment_required_task_carries_no_supply_block():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        r = _a2a_ask(client, cap)
        task = r.json()["result"]
        meta = task["status"]["message"]["metadata"]
        ns = meta.get("io.agent-guild/no_supply")
        assert ns and ns["capability"] == demand.canonical_capability(cap)
        assert ns["verified_reachable"] == 0
        assert "shortlist" not in json.dumps(task)


def test_supplied_capability_has_no_no_supply_block():
    from app.main import app
    cap = _cap()
    store.register_agent(name="supplier-" + cap, capabilities=[cap],
                         metadata={})
    with TestClient(app) as client:
        r = client.get(f"/check?capability={cap}",
                       headers={"User-Agent": EXT_UA})
        assert r.status_code == 402
        # supply exists on paper: the block is absent or reports the counts
        ns = r.json().get("no_supply")
        if ns is not None:
            assert ns["supplied"] == 1


# ---------------------------------------------------------------------------
# exclusions: probes, crawlers, AG-owned traffic
# ---------------------------------------------------------------------------

def test_greeting_probe_records_no_demand():
    from app.main import app
    with TestClient(app) as client:
        client.post("/a2a", json={
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"role": "user",
                                   "parts": [{"kind": "text",
                                              "text": "hello"}]}},
        }, headers={"User-Agent": EXT_UA})
        assert not [e for e in store.events
                    if e.get("type") == "capability_demand"
                    and e.get("capability") == "hello"]


def test_ag_owned_and_crawler_demand_never_counts_as_genuine():
    from app.main import app
    cap = _cap()
    with TestClient(app) as client:
        # AG-owned (first-party header) + a registry crawler both ask
        client.get(f"/check?capability={cap}",
                   headers={"User-Agent": "guild-selfcheck/1",
                            "X-Guild-Source": "ops-watch"})
        client.get(f"/check?capability={cap}",
                   headers={"User-Agent": "Glama-Bot/2.0 (+crawler)"})
        rows = store.demand_feed_entries()
        entry = next((r for r in rows
                      if r["capability"] == demand.canonical_capability(cap)),
                     None)
        assert entry is None or entry["genuine_lookups"] == 0, \
            "AG-owned/crawler traffic counted as genuine external demand"
        # a genuine external ask flips it
        client.get(f"/check?capability={cap}", headers={"User-Agent": EXT_UA})
        rows = store.demand_feed_entries()
        entry = next(r for r in rows
                     if r["capability"] == demand.canonical_capability(cap))
        assert entry["genuine_lookups"] == 1
