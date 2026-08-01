"""What an external MCP crawler learns from `tools/list`, asserted on the
ACTUAL served payload.

On 2026-08-01 an independent reliability probe (`mcp:glimind-probe/0.1.0`)
called `guild_paid_operations` once and did not request a challenge. Its public
health record for that tool read `known:false`, `payment.access=unknown`,
`payment.x402=false`, "Payment/access terms unknown — no pricing or payment
metadata advertised".

That was accurate. The served entry carried prose, an empty `inputSchema`, an
opaque `outputSchema` (`additionalProperties: true` and nothing else) and
`_meta: {"fastmcp": {"tags": []}}`. Everything a buyer needs was one free call
away, and nothing in the discovery surface said so in a form a parser reads.

These tests assert the discovery surface itself — the tool object exactly as
`tools/list` serves it — not a helper's return value. A helper copy can agree
with itself while the served payload says nothing.

VENDOR-NEUTRAL. Nothing here is conditioned on any client, user agent or
partner. The assertions describe what ANY crawler must be able to read.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import paidcatalog


def _served_tool(name: str = "guild_paid_operations") -> dict:
    """The tool object as `tools/list` serves it, via the real MCP server."""
    from app.mcp_server import mcp

    async def go():
        # `_list_tools()` is the server's own tool enumeration; `to_mcp_tool()`
        # renders each entry into the exact wire object a `tools/list` response
        # carries. Asserting on THIS, rather than on a helper's return value,
        # is the whole point: a helper can agree with itself while the served
        # payload says nothing.
        tools = await mcp._list_tools()
        t = [x for x in tools if getattr(x, "name", None) == name][0]
        wire = t.to_mcp_tool()
        return wire.model_dump(by_alias=True)
    return asyncio.run(go())


@pytest.fixture(scope="module")
def served():
    return _served_tool()


# --------------------------------------------------------------------------
# the six claims that must never disappear
# --------------------------------------------------------------------------
def test_the_description_names_evidence_bundle(served):
    assert "evidence_bundle" in served["description"]


def test_the_description_states_offline_verification_and_inclusion_proof(served):
    d = served["description"].lower()
    assert "offline-verifiable" in d or "offline verifiable" in d
    assert "inclusion proof" in d
    assert "checkpoint" in d
    assert "signed" in d


def test_the_description_states_x402_autonomy(served):
    d = served["description"].lower()
    assert "x402" in d
    assert "no account" in d
    for phrase in ("no human", "no sales"):
        if phrase in d:
            break
    else:
        pytest.fail("the description must state that payment needs no human")


def test_the_description_names_the_exact_entrypoint(served):
    """A crawler that reads only tools/list must still learn where the artefact
    comes from."""
    ep = {o["operation"]: o["entrypoint"]["path"]
          for o in paidcatalog.operations()}["evidence_bundle"]
    assert ep in served["description"], (
        f"the served description does not name {ep}")


def test_the_description_points_at_the_live_price_source_not_a_number(served):
    """Requirement: no mutable price frozen into static prose."""
    d = served["description"]
    assert "free" in d.lower()
    for op in paidcatalog.operations():
        assert op["price_usd"] not in d, (
            f"{op['operation']}'s price is baked into the tool description and "
            "will be stale the moment an experiment moves it")
    assert "$" not in d


def test_the_description_preserves_the_free_alternatives(served):
    d = served["description"].lower()
    assert "free" in d
    # every paid operation still has a free alternative in the response
    for op in paidcatalog.operations():
        assert op["free_alternative"].strip()


# --------------------------------------------------------------------------
# machine-readable fields
# --------------------------------------------------------------------------
def test_the_served_meta_declares_payment_terms(served):
    meta = served.get("_meta") or served.get("meta") or {}
    block = meta.get("ai.agent-guild/paid")
    assert block, f"no namespaced paid metadata in served _meta: {list(meta)}"
    assert block["payment_protocol"] == "x402"
    assert block["network"] == "eip155:8453"
    assert block["autonomous"] is True
    assert block["human_in_the_loop"] is False
    assert block["account_required"] is False
    assert set(block["operations"]) == {o["operation"]
                                        for o in paidcatalog.operations()}
    assert block["free_alternative_exists_for_every_operation"] is True


def test_the_served_meta_carries_no_price(served):
    """`_meta` is fixed at registration; a number here freezes at boot.

    Checked STRUCTURALLY, not by substring: "20" occurs inside "eip155:8453"
    and would give a false positive (it did, on the first draft of this test).
    A price could only arrive as a numeric leaf or a currency literal, so both
    are banned outright."""
    meta = served.get("_meta") or served.get("meta") or {}
    block = meta.get("ai.agent-guild/paid")

    def leaves(node):
        if isinstance(node, dict):
            for v in node.values():
                yield from leaves(v)
        elif isinstance(node, list):
            for v in node:
                yield from leaves(v)
        else:
            yield node

    numeric = [v for v in leaves(block)
               if isinstance(v, (int, float)) and not isinstance(v, bool)]
    assert not numeric, (
        f"numeric leaves in the served paid metadata {numeric} — a price here "
        "freezes at registration and goes stale the moment one moves")
    blob = json.dumps(block)
    assert "$" not in blob
    for op in paidcatalog.operations():
        assert op["price_usd"] not in blob


def test_the_served_meta_points_at_a_live_price_source(served):
    meta = served.get("_meta") or served.get("meta") or {}
    src = meta["ai.agent-guild/paid"]["price_source"]
    assert src["mcp_tool"] == "guild_paid_operations"
    assert src["http_catalog"].startswith("http")
    assert "src=paid_offer:registry" in src["http_catalog"]


def test_the_served_annotations_say_reading_the_catalogue_is_safe(served):
    """Standard MCP ToolAnnotations — not an invented payment annotation."""
    ann = served.get("annotations") or {}
    assert ann.get("readOnlyHint") is True
    assert ann.get("destructiveHint") is False
    assert ann.get("idempotentHint") is True
    assert ann.get("title")


def test_the_served_output_schema_is_no_longer_opaque(served):
    out = served.get("outputSchema") or {}
    props = out.get("properties") or {}
    assert "operations" in props, "outputSchema still tells a crawler nothing"
    item = props["operations"]["items"]["properties"]
    for field in ("operation", "price_usd", "entrypoint", "settlement",
                  "free_alternative"):
        assert field in item, f"outputSchema omits {field}"
    # permissive on purpose: an over-tight schema breaks real calls
    assert out.get("additionalProperties") is True


def test_no_vendor_or_partner_is_named_anywhere_in_the_surface(served):
    """One discovery variable, for every crawler. No partner special-casing."""
    blob = json.dumps(served).lower()
    for vendor in ("glimind", "partner", "oracle-specific"):
        assert vendor not in blob


# --------------------------------------------------------------------------
# the tool still works, and still tells the truth
# --------------------------------------------------------------------------
def test_the_free_call_still_returns_live_prices_and_matches_the_schema():
    import app.mcp_server as mcp_server
    fn = getattr(mcp_server.guild_paid_operations, "fn",
                 mcp_server.guild_paid_operations)
    body = fn(ctx=None)
    assert body["source"] == "paid_offer:mcp_tool"
    ops = {o["operation"]: o for o in body["operations"]}
    assert set(ops) == {o["operation"] for o in paidcatalog.operations()}
    live = {o["operation"]: o["price_credits"] for o in paidcatalog.operations()}
    for name, op in ops.items():
        assert op["price_credits"] == live[name]
        assert op["entrypoint"]["call"]
        assert op["free_alternative"].strip()


# --------------------------------------------------------------------------
# THE MEASUREMENT LADDER — proven, not extended
# --------------------------------------------------------------------------
# The experiment is only worth running if a 402 challenge can be joined back to
# the impression that preceded it. That attribution ALREADY EXISTS, so these
# tests prove it rather than adding fields:
#
#   impression  paid_offer_served  key + operation + source
#   challenge   paid_offer_shown   key + challenged_operation + impression tag
#
# The join key is (actor, operation). `source` belongs to the impression — the
# challenge is downstream of it — so nothing needs widening to answer "did the
# actor we showed evidence_bundle to on the MCP surface go on to be
# challenged for it?".
#
# An impression is $0. A challenge is $0. Both are asserted as such.
from app.store import Store  # noqa: E402


class _Ctx:
    pass


def _mcp_ctx(monkeypatch, store, ua):
    import app.mcp_server as mcp_server
    monkeypatch.setattr(mcp_server, "store", store, raising=False)
    monkeypatch.setattr(mcp_server, "_client_ua", lambda ctx: ua)
    return mcp_server


def test_challenge_is_attributable_to_the_same_actor_and_operation(monkeypatch):
    """Rung 1 -> rung 2 of the ladder, end to end on the MCP surface."""
    s = Store(path="")
    ua = "mcp:some-external-oracle/0.1.0"
    ms = _mcp_ctx(monkeypatch, s, ua)

    # rung 1 — the free catalogue read produces the impression
    fn = getattr(ms.guild_paid_operations, "fn", ms.guild_paid_operations)
    fn(ctx=None)
    impressions = [e for e in s.events if e["type"] == "paid_offer_served"]
    assert {e["operation"] for e in impressions} == {
        o["operation"] for o in paidcatalog.operations()}
    assert {e["source"] for e in impressions} == {"paid_offer:mcp_tool"}
    actor = impressions[0]["key"]
    assert actor.startswith("mcp:") and actor != "mcp"

    # rung 2 — a 402 challenge for one of those operations, same caller
    from app import payments
    ms._record_paid_offer(
        payments.evidence_bundle_request("https://example.invalid/a2a"),
        ctx=None)
    challenges = [e for e in s.events if e["type"] == "paid_offer_shown"]
    assert len(challenges) == 1
    c = challenges[0]
    assert c["challenged_operation"] == "evidence_bundle"
    assert c["impression"] == "challenge_402"
    assert c["transport"] == "mcp"

    # THE JOIN: same actor, same operation, no new field required
    assert c["key"] == actor, "the challenge is not attributable to the actor"
    matched = [e for e in impressions
               if e["key"] == c["key"]
               and e["operation"] == c["challenged_operation"]]
    assert len(matched) == 1
    assert matched[0]["source"] == "paid_offer:mcp_tool", (
        "the SOURCE of the impression is what tells us which surface produced "
        "the challenge; it lives on rung 1 by design")


def test_an_impression_and_a_challenge_are_both_still_zero_revenue(monkeypatch):
    s = Store(path="")
    ms = _mcp_ctx(monkeypatch, s, "mcp:some-external-oracle/0.1.0")
    fn = getattr(ms.guild_paid_operations, "fn", ms.guild_paid_operations)
    fn(ctx=None)
    from app import payments
    ms._record_paid_offer(
        payments.evidence_bundle_request("https://example.invalid/a2a"),
        ctx=None)

    from app import experiments
    metrics = experiments.commercial_metrics(s)
    assert metrics["external_settled_revenue_usd"] == 0.0
    assert metrics["distinct_external_payers"] == 0
    assert metrics["paid_decisions"] == 0


def test_our_own_probe_can_never_climb_the_ladder(monkeypatch):
    """Vendor-neutral in both directions: the surface is open to everyone, and
    our own tooling still cannot register as demand on it."""
    s = Store(path="")
    ms = _mcp_ctx(monkeypatch, s, "mcp:guild-live-conformance")
    fn = getattr(ms.guild_paid_operations, "fn", ms.guild_paid_operations)
    fn(ctx=None)
    assert s.paid_offer_funnel()["qualified_distinct_actors"] == 0
