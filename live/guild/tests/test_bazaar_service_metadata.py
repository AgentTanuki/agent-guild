"""High-quality x402 Bazaar metadata for autonomous buyers."""
from app import x402
from app import protecteddecision
from app.payments import (
    check_request, payment_decision_request,
    protected_payment_decision_request,
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
    assert output["example"]["verdict"] == "hire"
    assert "schema" not in output


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
