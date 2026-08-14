"""High-quality x402 Bazaar metadata for autonomous buyers."""
from jsonschema import Draft202012Validator

from app import x402
from app import protecteddecision
from app.payments import (
    check_request, deep_preflight_request, evidence_bundle_request,
    machine_envelope_request, payment_decision_request,
    protected_payment_decision_request, search_request,
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
