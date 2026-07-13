"""External-provider registration + Guild-observed delivery.

A registry-discovered third-party provider (which holds no Guild key) is marked
provenance=external / first_party=False, its terms recorded, and its work is
verified ONLY via a Guild-observed invocation of its real endpoint — never a
self-claim. Such a bound, observed delivery seals as guild_mediated with basis
guild_observed_invocation, and the provider is NEVER counted first-party."""
import os

os.environ["GUILD_DATA"] = ""

from app.store import Store  # noqa: E402
from app.ledger import Ledger  # noqa: E402


def test_external_provider_is_external_and_observed_delivery_is_guild_mediated():
    s = Store(path="")
    buyer = s.register_agent("Buyer", ["hiring"], metadata={})
    prov = s.register_external_provider(
        name="Hello World Agent", capabilities=["hello"],
        endpoint="https://hello.example.org",
        registry_source="a2aregistry.org",
        terms={"provider": {"organization": "A2A Registry Team"}},
        discovered_by=buyer["id"])
    # genuinely external, no key, terms recorded
    assert prov["first_party"] is False
    assert prov["external_provider"] is True
    assert prov["custodial"] is False
    assert prov.get("private_key") is None and not prov.get("api_key")
    assert prov["metadata"]["provider_terms"]["provider"]["organization"] == "A2A Registry Team"
    # idempotent per endpoint
    again = s.register_external_provider(
        name="dupe", capabilities=["hello"], endpoint="https://hello.example.org")
    assert again["id"] == prov["id"]

    # a Guild-observed bound invocation records the delivery
    s.agents[prov["id"]]["metadata"]["endpoint"] = "https://hello.example.org"
    task = s.create_task(buyer["id"], prov["id"], "hello", payment=3)
    inv = s.begin_outbound_invocation(prov["id"])
    assert inv is not None
    resp = '{"result":"Hello World!"}'
    import hashlib
    dh = "0x" + hashlib.sha256(resp.encode()).hexdigest()
    ok = s.complete_outbound_invocation(inv["invocation_id"], protocol_ok=True,
                                        receipt_ref=task["id"])
    assert ok
    s.submit_receipt(task["id"], dh, outcome="delivered",
                     receipt_auth="guild_observed")
    rec = s.append_task_to_ledger(task["id"])
    assert rec["provenance"] == "guild_mediated"
    assert rec["evidence"]["basis"] == "guild_observed_invocation"

    # the external provider never counts as first-party in reputation views
    led = Ledger.from_records(s.ledger_records)
    rep = led.derive_reputation().get(prov["id"])
    assert rep is not None
    assert "first_party_bootstrap" not in rep["by_provenance"]


def test_x402_disabled_body_labels_credits_sandbox():
    from app import x402
    body = x402.payment_required_body("best_agent", 10)
    assert body["accepts"] == []           # no treasury configured in tests
    assert body["sandbox"]["unit"] == "credits_sandbox"
    assert "x402_status" in body
