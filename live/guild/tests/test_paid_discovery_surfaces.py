"""Paid-layer machine discoverability + honest per-source impression telemetry.

Context (2026-08-01). The paid layer was live, priced, fail-closed and
x402-v2-conformant — and invisible. Verified that day against CDP's live
catalog: zero Bazaar entries for this host and zero for each of
deep_preflight / evidence_bundle / watch_cycle. The only documented route into
that catalog is a SETTLED payment through the CDP facilitator, which is
unreachable at $0 revenue. So the surfaces we control had to carry the offer,
and each one had to be individually measurable.

These tests assert the two things that can silently rot:
  1. every machine-readable surface actually carries the paid catalog, at the
     price the gateway will really charge (not a hard-coded copy);
  2. impressions are attributed per (operation, source) and our OWN traffic is
     excluded by caller class rather than by a filter someone can quietly drop.
"""
import os

os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app import paidcatalog, pricing  # noqa: E402
from app.main import app  # noqa: E402
from app.store import Store  # noqa: E402

client = TestClient(app)

PAID_OPS = {"best_agent", "signed_decision",
            "machine_envelope", "payment_decision",
            "protected_payment_decision", "deep_preflight",
            "evidence_bundle", "watch_cycle"}


def test_catalog_prices_come_from_the_gateway_not_a_copy(monkeypatch):
    """A surface must never advertise a price the gateway will not honour."""
    ops = {o["operation"]: o for o in paidcatalog.operations()}
    assert set(ops) == PAID_OPS
    for name, op in ops.items():
        if name == "protected_payment_decision":
            # Discovery carries the deterministic minimum; the exact body-
            # derived fee is quoted by the shared gateway and published in
            # dynamic_price. It is deliberately not in pricing.DEFAULTS.
            assert op["price_credits"] == 10
            assert op["dynamic_price"]["basis_points"] == 25
            assert op["dynamic_price"]["immutable"] is True
        else:
            assert op["price_credits"] == pricing.price(name)
        assert op["free_alternative"], f"{name} must name a free alternative"
        # NOTE: resource/route correctness is asserted in
        # tests/test_paid_catalog_binding.py against payments.*_request and the
        # real route table. A startswith("http") check used to live here and was
        # worthless — it passed while all three advertised signatures were
        # non-routes.

    # move the live price; the catalog must follow with no code change
    monkeypatch.setattr(pricing, "price",
                        lambda o: 4242 if o == "watch_cycle" else 7)
    moved = {o["operation"]: o for o in paidcatalog.operations()}
    assert moved["watch_cycle"]["price_credits"] == 4242
    assert moved["watch_cycle"]["price_usd"] == "$4.242"


def test_catalog_publishes_literal_buyer_intents_from_one_source():
    """Semantic selectors match buyer language, not Python function names."""
    ops = {o["operation"]: o for o in paidcatalog.operations()}
    for name, op in ops.items():
        assert len(op["buyer_intents"]) >= 2, name
        assert op["buyer_intents"] == paidcatalog.buyer_intents(name)
        assert all(isinstance(text, str) and len(text) >= 20
                   for text in op["buyer_intents"])

    assert "which agent should I hire for this capability" in \
        ops["best_agent"]["buyer_intents"]
    assert "sign a machine-to-machine message" in \
        ops["machine_envelope"]["buyer_intents"]
    assert "is this wallet safe to pay" in \
        ops["payment_decision"]["buyer_intents"]


def test_every_surface_carries_the_paid_catalog():
    """agent card, manifest, llms.txt — all three, or an agent has to trip a
    402 on a route it had no reason to call in order to learn we sell
    anything."""
    card = client.get("/.well-known/agent-card.json").json()
    block = card["x-agent-guild-paid-operations"]
    assert block["source"] == "paid_offer:agent_card"
    assert {o["operation"] for o in block["operations"]} == PAID_OPS
    assert all(o["buyer_intents"] for o in block["operations"])

    man = client.get("/.well-known/agent-guild.json").json()
    assert man["paid_operations"]["source"] == "paid_offer:manifest"
    assert {o["operation"] for o in man["paid_operations"]["operations"]} == PAID_OPS
    assert all(o["buyer_intents"]
               for o in man["paid_operations"]["operations"])

    txt = client.get("/llms.txt").text
    for name in PAID_OPS:
        assert name in txt, f"{name} missing from llms.txt"
    assert "free instead:" in txt
    assert "use when:" in txt
    assert "which agent should I hire for this capability" in txt
    assert "paid_offer:llms_txt" in txt


def test_primary_machine_discovery_surfaces_publish_one_call_envelope_client():
    """The profitable envelope must be executable from the documents agents
    actually crawl, not discoverable only after reading prose or hand-building
    a proof/payment stack."""
    card = client.get("/.well-known/agent-card.json").json()
    manifest = client.get("/.well-known/agent-guild.json").json()
    surfaced = (
        (card["x-agent-guild-paid-operations"],
         "http://testserver/sdk/agentguild_envelope_client.mjs"),
        (manifest["paid_operations"],
         "https://agent-guild-5d5r.onrender.com"
         "/sdk/agentguild_envelope_client.mjs"),
    )
    for block, expected in surfaced:
        envelope = {op["operation"]: op for op in block["operations"]}[
            "machine_envelope"]
        buyer = envelope["entrypoint"]["client"]
        assert buyer["source"] == expected
        assert buyer["factory"].startswith("createEvmMachineEnvelopeClient")
        assert buyer["operation"].startswith("client.issue")
        assert buyer["dependencies"] == ["@x402/fetch", "@x402/evm"]
        assert "caller control" in buyer["custody"]


def test_free_alternative_is_named_on_every_surface():
    """Honesty invariant: we would rather be the default than be paid once.
    A paid offer that hides its free sibling is a dark pattern."""
    card = client.get("/.well-known/agent-card.json").json()
    for op in card["x-agent-guild-paid-operations"]["operations"]:
        assert op["free_alternative"].strip()
    assert "free" in client.get("/llms.txt").text.lower()


def test_unknown_source_id_is_rejected():
    """Source ids are the axis the whole experiment reports on. A typo must
    fail loudly, not create a phantom surface with its own history."""
    import pytest
    with pytest.raises(ValueError):
        paidcatalog.offer_block("paid_offer:not_a_real_surface")


def test_clawhub_skill_source_is_closed_and_attributable():
    """An installed registry skill can identify its own acquisition channel,
    but an arbitrary caller cannot mint new source buckets."""
    assert "paid_offer:clawhub_skill" in paidcatalog.SOURCE_IDS

    tagged = client.get(
        "/.well-known/agent-guild.json",
        params={"src": "paid_offer:clawhub_skill"},
    ).json()
    assert tagged["paid_operations"]["source"] == \
        "paid_offer:clawhub_skill"

    untrusted = client.get(
        "/.well-known/agent-guild.json",
        params={"src": "paid_offer:made_up"},
    ).json()
    assert untrusted["paid_operations"]["source"] == \
        "paid_offer:manifest"


# --------------------------------------------------------------------------
# telemetry
# --------------------------------------------------------------------------
def _store_with(events):
    s = Store(path="")
    s.events = list(events)
    return s


def _ev(**kw):
    # UA NOTE (2026-08-01): a bare/empty user agent no longer QUALIFIES — it is
    # indistinguishable from our own traffic. These tests are about splitting
    # and exclusion, so the default fixture uses a recognised external
    # agent-framework UA; qualification itself is asserted in
    # tests/test_paid_offer_actor_binding.py.
    base = {"type": "paid_offer_served", "operation": "deep_preflight",
            "source": "paid_offer:agent_card", "key": "anon",
            "ua": "langchain/0.2.1",
            "at": "2026-08-01T00:00:00+00:00"}
    base.update(kw)
    return base


def test_impressions_split_by_operation_and_source():
    s = _store_with([
        _ev(key="actor-a", operation="deep_preflight",
            source="paid_offer:agent_card"),
        _ev(key="actor-b", operation="evidence_bundle",
            source="paid_offer:agent_card"),
        _ev(key="actor-a", operation="deep_preflight",
            source="paid_offer:llms_txt"),
    ])
    f = s.paid_offer_funnel()
    assert f["qualified_distinct_actors"] == 2
    assert f["by_operation"]["deep_preflight"]["qualified_distinct_actors"] == 1
    assert f["by_operation"]["evidence_bundle"]["qualified_distinct_actors"] == 1
    assert set(f["by_source"]) == {"paid_offer:agent_card",
                                   "paid_offer:llms_txt"}
    assert f["measurable"] is True


def test_our_own_traffic_is_excluded_structurally_not_by_ua_string():
    """The 2026-08-01 pollution: in-process scout loops fell through to
    EXTERNAL_UNKNOWN because they had no actor key and no header. A UA-matched
    exclusion is both spoofable and fragile; the origin stamp is neither."""
    s = _store_with([
        _ev(key="real-external"),
        _ev(key="anon", origin="swarm_scout"),      # our own loop
        _ev(key="anon", fp=True),                   # first-party header
        _ev(key="anon", ua="Glama/1.0 crawler"),    # registry crawler
    ])
    f = s.paid_offer_funnel()
    assert f["qualified_distinct_actors"] == 1
    src = f["by_source"]["paid_offer:agent_card"]
    assert src["impressions"] == 4
    assert src["not_qualified"] == 3
    assert src["qualified_impressions"] == 1


def test_internal_origin_is_first_party_by_construction():
    from app import attribution
    ev = {"type": "candidate_discovered", "key": "anon", "ua": "",
          "origin": "swarm_scout"}
    assert attribution.caller_class(ev) == "AG_INTERNAL"
    assert attribution.may_count_as_external_growth(
        attribution.caller_class(ev)) is False
    # and an OUTSIDE caller cannot launder itself in: origin is stamped at the
    # emit site, and an unrecognised value is not an internal origin.
    spoof = {"type": "x", "key": "anon", "ua": "", "origin": "swarm_scout_lol"}
    assert attribution.caller_class(spoof) == "EXTERNAL_UNKNOWN"


def test_unknown_internal_origin_raises_rather_than_silently_unclassified():
    import pytest
    s = Store(path="")
    with pytest.raises(ValueError):
        s.record_internal_event("whatever", "not_a_registered_origin")


def test_anonymous_serves_are_not_counted_as_failed_conversions():
    """An actor we cannot follow can neither convert nor fail to convert."""
    s = _store_with([_ev(key="anon"), _ev(key="anon"), _ev(key="linked")])
    f = s.paid_offer_funnel()
    assert f["anonymous_unlinkable_impressions"] == 2
    assert f["qualified_distinct_actors"] == 1


def test_zero_denominator_reports_not_measurable():
    f = Store(path="").paid_offer_funnel()
    assert f["qualified_distinct_actors"] == 0
    assert f["measurable"] is False
    assert "price is NOT the variable under test" in f["note"]


def test_funnel_paid_endpoint_is_served():
    r = client.get("/funnel/paid")
    assert r.status_code == 200
    body = r.json()
    assert "by_operation" in body and "by_source" in body
    assert "qualified_distinct_actors" in body
