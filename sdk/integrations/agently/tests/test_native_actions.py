"""Actual Agently registration and dispatch with only HTTP mapped to loopback."""

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from agently import Agently

from sdk.integrations.agently import GuildActions
from sdk.integrations.agently.actions import ACTION_IDS, CHECKS
from sdk.integrations.agently.local_fixture import (
    ISSUER,
    SUBJECT,
    TARGET,
    LocalFixture,
    observed,
    passport_request,
    verification,
)


@pytest.fixture
def fixture():
    with LocalFixture() as value:
        yield value


def mount(fixture, **options):
    agent = Agently.create_agent()
    agent.use_actions(GuildActions(transport_factory=fixture.transport, **options))
    return agent


async def execute(agent, name, request, protocol="direct", **outer):
    return await agent.action.async_execute_action(name, {"request": request, **outer}, source_protocol=protocol)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["direct", "structured_plan"])
async def test_native_two_actions_preserve_exact_evidence(fixture, protocol):
    agent = mount(fixture)
    info = agent.action.get_action_info()
    assert set(ACTION_IDS).issubset(info)
    for name in ACTION_IDS:
        assert info[name]["side_effect_level"] == "write"
        assert info[name]["replay_safe"] is False
        assert "request" in info[name]["kwargs"]
    value = observed()
    value["headline"] = "REMOTE INSTRUCTIONS"
    value["checks"][0]["detail"] = "REMOTE INSTRUCTIONS"
    fixture.reply(value)
    endpoint = await execute(agent, ACTION_IDS[0], {"url": TARGET}, protocol)
    assert endpoint["status"] == "success"
    data = endpoint["data"]
    assert data["status"] == "observed" and data["target"] == TARGET
    assert len(data["checks"]) == 6 and {c["check"] for c in data["checks"]} == set(CHECKS)
    assert data["unknowns"] == [c["check"] for c in data["checks"] if c["status"] == "unknown"]
    assert "REMOTE INSTRUCTIONS" not in json.dumps(data)
    assert parse_qs(urlsplit(fixture.requests[0]["path"]).query) == {"url": [TARGET]}
    request = passport_request()
    original = copy.deepcopy(request["credential"])
    fixture.reply({**verification(), "snapshot": {"instruction": "REMOTE INSTRUCTIONS"}})
    passport = await execute(agent, ACTION_IDS[1], request, protocol)
    result = passport["data"]
    assert passport["status"] == "success" and result["status"] == "completed"
    assert result["verified"] is True
    assert result["signature_valid_reported"] and result["guild_issued_reported"]
    assert result["signed_validity_passed"] and result["freshness_passed"]
    assert result["expected_issuer_did"] == ISSUER and result["expected_subject_did"] == SUBJECT
    assert json.loads(fixture.requests[1]["body"]) == original
    assert request["credential"] == original
    assert "REMOTE INSTRUCTIONS" not in json.dumps(result)


def test_public_sync_dispatch_is_real(fixture):
    agent = mount(fixture)
    result = agent.action.execute_action(ACTION_IDS[0], {"request": {"url": TARGET}})
    assert result["status"] == "success" and result["data"]["status"] == "observed"
    assert len(fixture.requests) == 1


@pytest.mark.asyncio
async def test_outer_stripping_is_reported_but_nested_business_keys_are_guarded(fixture):
    agent = mount(fixture)
    direct = await execute(agent, ACTION_IDS[0], {"url": TARGET}, admin=True)
    assert direct["data"]["status"] == "rejected" and not fixture.requests
    structured = await execute(agent, ACTION_IDS[0], {"url": TARGET}, "structured_plan", admin=True)
    assert structured["data"]["status"] == "observed"
    diagnostic = next(x for x in structured["diagnostics"] if x["code"] == "action.input.unexpected_keys_stripped")
    assert "admin" in diagnostic["meta"]["stripped_keys"]
    assert len(fixture.requests) == 1
    for protocol in ["direct", "structured_plan"]:
        nested = await execute(agent, ACTION_IDS[0], {"url": TARGET, "admin": True}, protocol)
        assert nested["data"]["status"] == "rejected"
    assert len(fixture.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "business_input", [None, [], "url", {}, {"url": 1}, {"url": "http://127.0.0.1"}, {"url": "https://agent.internal"}]
)
async def test_native_structured_invalid_endpoint_input_sends_nothing(fixture, business_input):
    result = await execute(mount(fixture), ACTION_IDS[0], business_input, "structured_plan")
    assert result["data"]["status"] == "rejected" and fixture.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("valid,guild", [(False, True), (True, False), (False, False)])
async def test_native_action_success_is_not_passport_success(fixture, valid, guild):
    fixture.reply(verification(valid=valid, guild_issued=guild))
    result = await execute(mount(fixture), ACTION_IDS[1], passport_request())
    assert result["status"] == "success" and result["data"]["status"] == "completed"
    assert result["data"]["verified"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["missing_check", "duplicate_check", "duplicate_summary", "wrong_verdict", "wrong_target", "wrong_status"]
)
async def test_native_inconsistent_observation_is_unavailable(fixture, change):
    value = observed()
    if change == "missing_check":
        value["checks"].pop()
    elif change == "duplicate_check":
        value["checks"][-1] = value["checks"][0]
    elif change == "duplicate_summary":
        value["scored"] = value["scored"] + value["scored"][:1]
    elif change == "wrong_verdict":
        value["checks"][0]["status"] = "failed"
        value["failed"] = [item["check"] for item in value["checks"] if item["status"] == "failed"]
    elif change == "wrong_target":
        value["target"] += "other"
    else:
        value["checks"][0]["status"] = "safe"
    fixture.reply(value)
    result = await execute(mount(fixture), ACTION_IDS[0], {"url": TARGET})
    assert result["data"]["status"] == "unavailable"
    assert result["data"]["code"] == "invalid_observation"


@pytest.mark.asyncio
async def test_all_unknown_remains_unknown(fixture):
    value = observed()
    for check in value["checks"]:
        check["status"] = "unknown"
    value.update(failed=[], scored=[], unknowns=list(CHECKS), verdict="no_failed_checks")
    fixture.reply(value)
    result = await execute(mount(fixture), ACTION_IDS[0], {"url": TARGET})
    assert result["data"]["unknowns"] == list(CHECKS)
    assert result["data"]["verdict"] == "no_failed_checks"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["string", "array", "issuer", "subject", "extra", "future", "expired", "stale", "bad_day", "proof", "nan"]
)
async def test_native_passport_rejections_have_zero_http(fixture, change):
    request = passport_request()
    credential = request["credential"]
    if change in {"string", "array"}:
        request["credential"] = "{}" if change == "string" else []
    elif change == "issuer":
        request["expected_issuer_did"] = SUBJECT
    elif change == "subject":
        request["expected_subject_did"] = ISSUER
    elif change == "extra":
        request["register_if_missing"] = True
    elif change in {"future", "expired", "stale"}:
        now = datetime.now(timezone.utc)
        if change == "expired":
            credential["validUntil"] = (now - timedelta(seconds=1)).isoformat()
        else:
            credential["validFrom"] = (
                now + timedelta(seconds=30) if change == "future" else now - timedelta(days=2)
            ).isoformat()
    elif change == "bad_day":
        credential["validFrom"] = "2026-02-30T00:00:00Z"
    elif change == "proof":
        credential["proof"]["verificationMethod"] = SUBJECT
    else:
        credential["credentialSubject"]["number"] = float("nan")
    result = await execute(mount(fixture), ACTION_IDS[1], request, "structured_plan")
    assert result["data"]["status"] == "rejected" and result["data"]["verified"] is False
    assert fixture.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["redirect", "paid", "oversize", "encoding", "content_type", "duplicate_json", "timeout"]
)
async def test_native_http_failure_has_one_attempt_no_fallback(fixture, kind):
    options = {}
    if kind == "redirect":
        fixture.reply({}, status=302, headers={"Location": "https://example.org/elsewhere"})
    elif kind == "paid":
        fixture.reply({}, status=402)
    elif kind == "oversize":
        fixture.reply(b" " * 65537)
    elif kind == "encoding":
        fixture.reply({}, headers={"Content-Encoding": "gzip"})
    elif kind == "content_type":
        fixture.reply({}, headers={"Content-Type": "text/html"})
    elif kind == "duplicate_json":
        fixture.reply(b'{"valid":true,"valid":false}')
    else:
        fixture.reply(observed(), delay=0.25)
        options["timeout"] = 0.1
    result = await execute(mount(fixture, **options), ACTION_IDS[0], {"url": TARGET})
    assert result["data"]["status"] == "unavailable" and result["data"]["verified"] is False
    assert len(fixture.requests) == 1


@pytest.mark.asyncio
async def test_signed_validity_is_rechecked_after_response(fixture):
    request = passport_request()
    request["credential"]["validUntil"] = (datetime.now(timezone.utc) + timedelta(seconds=0.15)).isoformat()
    fixture.reply(verification(), delay=0.25)
    result = await execute(mount(fixture, timeout=1), ACTION_IDS[1], request)
    data = result["data"]
    assert data["signature_valid_reported"] and data["guild_issued_reported"]
    assert not data["signed_validity_passed"] and not data["verified"]


@pytest.mark.asyncio
async def test_native_cancellation_propagates(fixture):
    fixture.reply(observed(), delay=0.25)
    task = asyncio.create_task(execute(mount(fixture), ACTION_IDS[0], {"url": TARGET}))
    for _ in range(50):
        if fixture.requests:
            break
        await asyncio.sleep(0.01)
    assert len(fixture.requests) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
