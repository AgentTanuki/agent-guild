"""Fraud flag discovery must describe the selected route, not agent hiring."""
import pytest

from app import a2a_x402, paidcatalog, payments, x402


@pytest.mark.parametrize(
    "preq, expected_intent, other_scope",
    [
        (
            payments.flags_request(0.7),
            "list agents at or above a minimum collusion suspicion score",
            "one agent",
        ),
        (
            payments.agent_flags_request("fixture-agent"),
            "inspect collusion suspicion and reasons for one agent",
            "minimum collusion suspicion",
        ),
    ],
)
def test_fraud_resource_describes_the_selected_flag_route(
    preq, expected_intent, other_scope
):
    description = x402.resource_info(preq).description
    assert "fraud_check" in description
    assert "collusion suspicion" in description
    assert expected_intent in description
    assert other_scope not in description
    for hiring_promise in ("hire", "safest agent", "capability", "rank agents"):
        assert hiring_promise not in description


def test_legacy_fraud_challenge_also_uses_the_fraud_label():
    preq = payments.agent_flags_request("fixture-agent")
    required = a2a_x402.payment_required_response(preq, preq.cost)
    description = required["accepts"][0]["description"]
    assert "collusion suspicion" in description
    assert "hire" not in description


def test_fraud_intents_do_not_guess_a_route():
    assert paidcatalog.buyer_intents("fraud_check") == []
    assert paidcatalog.buyer_intents("fraud_check", path="/future-flags") == []


def test_search_still_describes_capability_hiring():
    description = x402.resource_info(payments.search_request("fact-check")).description
    assert "which agent should I hire for this capability" in description
    assert "collusion suspicion" not in description
