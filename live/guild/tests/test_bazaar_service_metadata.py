"""High-quality x402 Bazaar metadata for autonomous buyers."""
from jsonschema import Draft202012Validator

from app import openapi_payment_discovery, x402
from app import protecteddecision
from app.payments import (
    PaidRequest,
    check_request, deep_preflight_request, evidence_bundle_request,
    machine_envelope_request, marketplace_signed_decision_request,
    payment_decision_request, protected_payment_decision_request,
    protected_payment_tier_request, search_request, watch_cycle_request,
)
from x402.extensions.bazaar import (
    extract_discovery_info_from_extension,
    validate_discovery_extension,
    validate_discovery_extension_spec,
)


def test_signed_decision_has_named_search_metadata():
    info = x402.resource_info(check_request(
        "fact-check", signed=True, ttl_seconds=3600))
    wire = info.model_dump(by_alias=True, exclude_none=True)

    assert wire["serviceName"] == "Agent Guild"
    assert wire["iconUrl"] == x402.public_host() + "/badge.svg"
    assert 1 <= len(wire["tags"]) <= 5
    assert {"agent-trust", "signed-proof", "x402"} <= set(wire["tags"])


def test_signed_decision_declares_truthful_output_contract():
    extension = x402.bazaar_extension(check_request(
        "fact-check", signed=True, ttl_seconds=3600))
    output = extension["info"]["output"]
    example = output["example"]
    schema = output["schema"]

    assert example["type"] == "AgentGuildDecision"
    assert example["contract"] == "AGD-1/1.0"
    assert example["issuer"].startswith("did:key:")
    assert example["decision"]["agent_id"] == example["routing"]["provider_id"]
    assert example["proof"]["cryptosuite"] == "eddsa-jcs-2022"
    assert example["valid_until"] > example["issued_at"]
    assert set(schema["required"]) >= {
        "type", "contract", "issuer", "capability", "decision", "routing", "proof"}
    assert schema["properties"]["decision"]["type"] == ["object", "null"]

    # Validate with the pinned official SDK, not just our own assertions. This
    # is the exact parser Coinbase's facilitator family is built to consume.
    assert validate_discovery_extension(extension).valid
    assert validate_discovery_extension_spec(extension).valid
    parsed = extract_discovery_info_from_extension(extension)
    assert parsed.input.method == "GET"
    assert parsed.input.query_params["signed"] == "true"
    assert parsed.output.model_extra["schema"]["properties"]["contract"][
        "const"] == "AGD-1/1.0"


def test_other_paid_products_receive_operation_specific_tags_without_schema_drift():
    decision = x402.resource_info(payment_decision_request("a" * 64))
    assert {"payment-policy", "wallet-security", "x402"} <= set(decision.tags)

    ordinary = check_request("code-review")
    output = x402.bazaar_extension(ordinary)["info"]["output"]
    # The unpaid 402 declares only a truthful outer response prefix. The paid
    # recommendation, shortlist and proof never ride inside discovery metadata.
    assert output["example"] == {
        "schema_version": 2, "capability": "fact-check", "status": "supply"}
    assert output["schema"]["properties"]["schema_version"]["const"] == 2
    assert "shortlist" not in str(output)
    assert "decision" not in str(output)


def test_search_declares_its_ranked_result_contract_without_changing_check():
    search_output = x402.bazaar_extension(
        search_request("fact-check"))["info"]["output"]
    check_output = x402.bazaar_extension(
        check_request("fact-check"))["info"]["output"]

    assert set(search_output["schema"]["required"]) == {
        "capability", "count", "results"}
    assert set(search_output["schema"]["properties"]["results"][
        "items"]["required"]) >= {
            "id", "did", "name", "capabilities", "trust", "rank",
            "confidence", "attestations_received"}
    assert set(check_output["schema"]["required"]) == {
        "schema_version", "capability", "status"}


def test_flagship_bazaar_examples_match_declared_schemas_and_official_sdk():
    requests = [
        check_request("fact-check"),
        search_request("fact-check"),
        check_request("fact-check", signed=True, ttl_seconds=3600),
        machine_envelope_request("a" * 64),
        payment_decision_request("b" * 64),
        protected_payment_decision_request(
            "c" * 64, protecteddecision.discovery_quote()),
        deep_preflight_request("https://agent.example/a2a"),
        evidence_bundle_request("https://agent.example/a2a"),
    ]
    for preq in requests:
        extension = x402.bazaar_extension(preq)
        assert validate_discovery_extension(extension).valid, preq.operation
        assert validate_discovery_extension_spec(extension).valid, preq.operation
        output = extension["info"]["output"]
        assert output["example"], preq.operation
        assert output["schema"], preq.operation
        Draft202012Validator(output["schema"]).validate(output["example"])


def test_body_routes_publish_official_bazaar_json_input_contract():
    from app import protectedmarket

    tier_id = "1000-usdc"
    requests = [
        marketplace_signed_decision_request("d" * 64),
        evidence_bundle_request("https://agent.example/a2a"),
        machine_envelope_request("a" * 64),
        payment_decision_request("b" * 64),
        protected_payment_decision_request(
            "c" * 64, protecteddecision.discovery_quote()),
        protected_payment_tier_request(
            tier_id, "e" * 64, protectedmarket.tier_quote(tier_id)),
        watch_cycle_request("https://agent.example/a2a"),
    ]
    advertised_posts = {
        (path, method) for path, method in
        openapi_payment_discovery.advertised_operations()
        if method == "post"
    }
    covered_posts = set()
    for preq in requests:
        extension = x402.bazaar_extension(preq)
        assert validate_discovery_extension(extension).valid, preq.path
        assert validate_discovery_extension_spec(extension).valid, preq.path
        Draft202012Validator(extension["schema"]).validate(extension["info"])
        parsed = extract_discovery_info_from_extension(extension)
        assert parsed.input.method == "POST"
        assert parsed.input.body_type == "json"
        assert parsed.input.body
        assert "queryParams" not in extension["info"]["input"]
        if preq.path != "/watch/cycle":
            path = preq.path
            if path.startswith("/wallet-binding/protected-decision/tiers/"):
                path = "/wallet-binding/protected-decision/tiers/{tier_id}"
            covered_posts.add((path, preq.method.lower()))

    # This is the POST equivalent of the origin-manifest partition test: a
    # newly advertised paid body route must add a non-empty Bazaar example in
    # this release-gating matrix before it can ship.
    assert covered_posts == advertised_posts

    envelope = x402.bazaar_extension(
        machine_envelope_request("a" * 64))["info"]["input"]["body"]
    assert set(envelope) == {"request", "caller_proof"}
    assert envelope["request"]["payload_sha256"] == "ab" * 32

    evidence = x402.bazaar_extension(
        evidence_bundle_request("https://agent.example/a2a"))["info"][
            "input"]["body"]
    assert evidence == {
        "url": "https://agent.example/a2a", "ttl_seconds": 3600}

    payment = x402.bazaar_extension(
        payment_decision_request("b" * 64))["info"]["input"]["body"]
    assert "payment" in payment
    assert "caller_proof" not in payment


def test_every_protected_tier_example_binds_its_own_amount_and_payan_offer():
    from app import protectedmarket

    assert set(protectedmarket.TIERS) == set(protectedmarket.PAYAN_TIER_OFFERS)
    for tier_id, amount in protectedmarket.TIERS.items():
        preq = protected_payment_tier_request(
            tier_id, "e" * 64, protectedmarket.tier_quote(tier_id))
        body = x402.bazaar_extension(preq)["info"]["input"]["body"]
        request = body["request"]
        buy_url = (
            f"{protectedmarket.PAYAN_ORIGIN}/x402/"
            f"{protectedmarket.PAYAN_TIER_OFFERS[tier_id]}")
        assert request["payment"]["amount"] == amount
        assert request["x402_resource_url"] == buy_url
        assert body["caller_proof"]["payload"]["resource"] == preq.path
        # These URLs MUST differ: payment.resource is the external transfer
        # being protected; x402_resource_url is the separate Guild service-fee
        # relay through which this decision is bought.
        assert request["payment"]["resource"] != buy_url


def test_get_bazaar_contract_remains_query_parameter_input():
    extension = x402.bazaar_extension(search_request("fact-check"))
    info = extension["info"]["input"]
    assert info == {
        "type": "http",
        "method": "GET",
        "queryParams": {"capability": "fact-check", "limit": "20",
                        "min_trust": "0"},
    }
    assert "bodyType" not in info


def test_unknown_future_post_cannot_turn_payment_challenge_into_500():
    extension = x402.bazaar_extension(PaidRequest(
        operation="best_agent", method="POST", path="/future-paid-product"))
    info = extension["info"]["input"]
    assert info == {
        "type": "http", "method": "POST", "bodyType": "json", "body": {}}
    assert validate_discovery_extension_spec(extension).valid


def test_x402_resource_copy_contains_literal_machine_selection_intent():
    best = x402.resource_info(check_request("fact-check"))
    assert "which agent should I hire for this capability" in best.description
    payment = x402.resource_info(payment_decision_request("a" * 64))
    assert "is this wallet safe to pay" in payment.description


def test_protected_value_decision_has_value_at_risk_discovery_metadata():
    preq = protected_payment_decision_request(
        "a" * 64, protecteddecision.discovery_quote())
    info = x402.resource_info(preq)
    assert {"payment-policy", "value-at-risk", "x402"} <= set(info.tags)
    extension = x402.bazaar_extension(preq)
    protection = extension["info"]["output"]["example"][
        "credentialSubject"]["protection"]
    assert protection["contract"] == "agent-guild/protected-value-policy/v1"
    assert protection["pricing"]["basis_points"] == 25
