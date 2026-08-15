"""The paid catalog must describe calls a buyer can actually execute.

An earlier revision of app/paidcatalog.py advertised three call signatures that
did not exist:

  * deep_preflight  as GET /check?capability=...&deep=true   (real: GET /preflight/deep?url=...)
  * evidence_bundle as GET /evidence/bundle?agent_id=...     (real: POST /evidence/bundle, JSON body)
  * watch_cycle     as POST /demand/watch?capability=...     (real: no callable route at all;
                                                              provisioning is POST /watch)

Every one of them passed the old assertion `resource.startswith("http")`, which
is why that assertion was worthless. These tests DERIVE the expectation from the
code that actually charges — `payments.*_request` — and from the app's real
route table, and then exercise the route.
"""
import json
import os

os.environ["GUILD_DATA"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import paidcatalog, payments  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)
PAID_OPS = {"machine_envelope", "payment_decision",
            "protected_payment_decision", "deep_preflight",
            "evidence_bundle", "watch_cycle",
            "best_agent", "signed_decision"}


def _routes():
    """(method, path) pairs the app really serves."""
    out = set()
    for r in app.routes:
        for m in getattr(r, "methods", None) or ():
            out.add((m, getattr(r, "path", "")))
    return out


def test_every_entrypoint_is_a_route_the_app_actually_serves():
    routes = _routes()
    for op in paidcatalog.operations():
        ep = op["entrypoint"]
        assert (ep["method"], ep["path"]) in routes, (
            f"{op['operation']} advertises {ep['method']} {ep['path']}, which "
            f"this app does not serve")


def test_settlement_resource_is_derived_from_the_real_payment_builder():
    """The catalog must not restate the binding — it must read it from the
    builder the gateway uses, so a builder change cannot leave the catalog
    advertising a stale settlement resource."""
    for op in paidcatalog.operations():
        preq = paidcatalog._settlement_request(op["operation"])
        assert op["settlement"]["canonical_resource"] == preq.resource_url
        assert op["settlement"]["method"] == preq.method


@pytest.mark.parametrize("name", sorted(PAID_OPS))
def test_entrypoint_parameters_match_what_the_route_requires(name):
    """Parameters must be in the right PLACE. The settlement resource
    canonicalises everything into a query string; the call may not."""
    op = {o["operation"]: o for o in paidcatalog.operations()}[name]
    ep = op["entrypoint"]
    preq = paidcatalog._settlement_request(name)
    binding_params = {k for k, _ in preq.query}

    supplied = set(ep.get("query_params") or {}) | set(ep.get("body") or {})
    if name == "watch_cycle":
        # provisioning entrypoint, deliberately a DIFFERENT surface from the
        # billing unit — asserted separately below.
        assert ep["directly_callable"] is False
        assert supplied == {"url", "interval_s"}
        return
    server_derived = set(ep.get("server_derived_settlement_params") or {})
    missing = binding_params - supplied - server_derived
    assert not missing, (
        f"{name}: the advertised call omits {missing}, which the settlement "
        f"binding requires")


def test_machine_envelope_binding_is_server_derived_and_private():
    op = {o["operation"]: o for o in paidcatalog.operations()}[
        "machine_envelope"]
    ep = op["entrypoint"]
    assert ep["caller_proof_required"] is True
    assert ep["server_derived_settlement_params"] == ["request_sha256"]
    assert "payload_sha256" in ep["body"]
    # The settlement URL binds an opaque normalized-request digest, not the
    # recipient, message digest or context themselves.
    resource = op["settlement"]["canonical_resource"]
    assert "request_sha256=" in resource
    assert "recipient" not in resource and "payload_sha256" not in resource


def test_payment_decision_binding_is_server_derived_and_exact():
    op = {o["operation"]: o for o in paidcatalog.operations()}[
        "payment_decision"]
    ep = op["entrypoint"]
    assert ep["server_derived_settlement_params"] == ["request_sha256"]
    assert set(ep["body"]["payment"]) == {
        "scheme", "network", "asset", "amount", "pay_to", "resource"}
    resource = op["settlement"]["canonical_resource"]
    assert "request_sha256=" in resource
    assert "pay_to" not in resource and "amount" not in resource


def test_protected_decision_is_dynamic_and_quote_bound():
    op = {o["operation"]: o for o in paidcatalog.operations()}[
        "protected_payment_decision"]
    ep = op["entrypoint"]
    assert ep["caller_proof_required"] is True
    assert ep["server_derived_settlement_params"] == [
        "request_sha256", "pricing", "fee_bps", "fee_credits"]
    assert op["dynamic_price"]["basis_points"] == 25
    assert op["dynamic_price"]["minimum_usd"] == 0.01
    assert op["dynamic_price"]["maximum_usd"] == 10000
    assert op["price_credits"] == 10  # discovery floor, not a fixed price
    resource = op["settlement"]["canonical_resource"]
    assert "fee_bps=25" in resource and "fee_credits=10" in resource
    assert "protected_usdc_atomic" not in resource


def test_deep_preflight_entrypoint_actually_challenges():
    """Call the advertised entrypoint verbatim. A paid route must answer with a
    402 challenge (or serve it) — never 404/405, which is what advertising a
    non-route produces."""
    op = {o["operation"]: o for o in paidcatalog.operations()}["deep_preflight"]
    ep = op["entrypoint"]
    r = client.request(ep["method"], ep["path"],
                       params={"url": "https://example.invalid/a2a"})
    assert r.status_code not in (404, 405), (
        f"advertised entrypoint {ep['method']} {ep['path']} is not callable: "
        f"{r.status_code}")


def test_evidence_bundle_entrypoint_accepts_a_body_not_a_query_string():
    """The real route reads JSON body. Sending the canonicalised settlement
    query string instead must be visibly wrong — this is the exact mistake the
    old catalog told buyers to make."""
    op = {o["operation"]: o for o in paidcatalog.operations()}["evidence_bundle"]
    ep = op["entrypoint"]
    assert ep["body"] is not None and ep["query_params"] is None

    ok = client.request(ep["method"], ep["path"],
                        json={"url": "https://example.invalid/a2a"})
    assert ok.status_code not in (404, 405)
    # the settlement form is NOT a call: no body -> the route rejects it
    bad = client.request(ep["method"], ep["path"],
                         params={"url": "https://example.invalid/a2a"})
    assert bad.status_code in (400, 422), (
        "query-string form should not be presented as callable")
    assert op["settlement"]["differs_from_entrypoint"] is True


def test_evidence_bundle_catalogue_binds_every_result_affecting_body_field():
    op = {o["operation"]: o for o in paidcatalog.operations()}[
        "evidence_bundle"]
    ep = op["entrypoint"]
    assert set(ep["body"]) == {"url", "ttl_seconds", "audience"}
    assert ep["server_derived_settlement_params"] == ["request_sha256"]
    resource = op["settlement"]["canonical_resource"]
    assert "request_sha256=" in resource
    assert "audience=" not in resource


def test_watch_cycle_is_labelled_not_directly_callable_and_routes_the_machine():
    """watch_cycle has NO public HTTP route. Saying so, and naming the flow that
    does exist, is the only honest option."""
    op = {o["operation"]: o for o in paidcatalog.operations()}["watch_cycle"]
    assert op["directly_callable"] is False
    assert "NOT directly callable" in op["callable_note"]
    # the billing resource is genuinely absent from the route table…
    assert ("POST", "/watch/cycle") not in _routes()
    # …and the entrypoint we DO advertise is the real provisioning route.
    assert (op["entrypoint"]["method"], op["entrypoint"]["path"]) in _routes()
    assert "mcp" in op["alternatives"]


def test_provisioning_entrypoint_is_callable_and_states_its_auth():
    """POST /watch requires a billing key; the catalog must say so rather than
    letting a machine discover it as a 401."""
    op = {o["operation"]: o for o in paidcatalog.operations()}["watch_cycle"]
    ep = op["entrypoint"]
    assert "X-API-Key REQUIRED" in ep["auth"]
    r = client.request(ep["method"], ep["path"],
                       json={"url": "https://example.invalid/a2a"})
    assert r.status_code == 401          # callable, and fails exactly as documented
    assert r.status_code not in (404, 405)


def test_llms_txt_prints_the_callable_form_not_the_binding_identifier():
    txt = client.get("/llms.txt").text
    assert "GET https://" in txt or "  call:" in txt
    assert "/preflight/deep" in txt
    assert "/evidence/bundle" in txt
    assert "NOT directly callable" in txt      # watch_cycle told the truth
    assert "binding identifier, not a call" in txt
    # the old, false signatures must never reappear
    assert "capability=<capability>&deep=true" not in txt
    assert "/demand/watch?capability=" not in txt


# --------------------------------------------------------------------------
# The block-level auth statement must not contradict any operation's own
# --------------------------------------------------------------------------
# `offer_block` used to assert, at the top level, "No account, no subscription,
# no human in the loop" — while watch_cycle's own entrypoint correctly says
# X-API-Key REQUIRED. A machine that believed the top-level line would call
# POST /watch and receive a 401 it had been told could not happen. A discovery
# surface that is wrong about its own auth is worse than one that says nothing.
def test_block_auth_statement_does_not_contradict_any_operation():
    block = paidcatalog.offer_block("paid_offer:manifest")
    auth = block["authentication"]

    # Read the STRUCTURED flag, never the prose. Parsing prose is how the
    # contradiction arose: deep_preflight's line says "none required", which a
    # naive uppercase match reads as REQUIRED.
    requires_key = {o["operation"] for o in block["operations"]
                    if o["entrypoint"]["key_required"]}
    keyless = {o["operation"] for o in block["operations"]
               if not o["entrypoint"]["key_required"]}
    assert set(auth["key_required_operations"]) == requires_key
    assert set(auth["keyless_operations"]) == keyless
    assert requires_key, "fixture invalid: no operation requires a key"

    # every keyless operation is named in the keyless claim...
    for name in keyless:
        assert name in auth["one_off_operations"], (
            f"{name} is keyless but the block does not say so")
    # ...and no key-requiring operation is claimed as keyless.
    for name in requires_key:
        assert name not in auth["one_off_operations"], (
            f"{name} REQUIRES a key but is listed under the keyless claim")
        assert name in auth["watches"]

    blob = json.dumps(block)
    assert "No account, no subscription" not in blob, (
        "the blanket no-account claim is back and contradicts watch_cycle")


def test_key_requirement_is_stated_wherever_the_operation_appears():
    """Whatever the top-level block says, each operation still carries its own
    exact auth line — the two must agree, not substitute for each other."""
    ops = {o["operation"]: o for o in paidcatalog.operations()}
    assert "X-API-Key REQUIRED" in ops["watch_cycle"]["entrypoint"]["auth"]
    for name in ("deep_preflight", "evidence_bundle"):
        assert "none required" in ops[name]["entrypoint"]["auth"]


def test_self_serve_route_to_a_key_is_named():
    """'Needs an account' is only honest if the machine is told how to get one
    without a human."""
    auth = paidcatalog.offer_block("paid_offer:manifest")["authentication"]
    assert "/billing/trial" in auth["watches"]
    assert "no human" in auth["watches"].lower()
