"""Machine-first response: compact, deterministic, hash-bound, non-imperative."""
from __future__ import annotations

import hashlib
import json
import os

os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def _send(text: str) -> tuple[dict, bytes]:
    response = client.post("/a2a", json={
        "jsonrpc": "2.0", "id": 808, "method": "message/send",
        "params": {"message": {"parts": [{"kind": "text", "text": text}]}},
    })
    assert response.status_code == 200
    raw = response.json()["result"]["parts"][0]["text"].encode()
    return json.loads(raw), raw


def _assert_binding(payload: dict, original: str):
    assert payload["request"] == {
        "sha256": hashlib.sha256(original.encode()).hexdigest(),
        "utf8_bytes": len(original.encode()),
    }


def test_default_probe_is_under_one_kilobyte_and_actions_disclose_effects():
    payload, raw = _send("hello")
    assert len(raw) < 1024
    assert payload["schema"] == "AGFC-1/1.0"
    assert payload["kind"] == "probe_ack"
    _assert_binding(payload, "hello")
    assert "supplied_capabilities" not in payload
    assert "self_description" not in payload
    assert "register_now" not in payload
    actions = {action["id"]: action for action in payload["available_actions"]}
    assert actions["trust.check"]["effect"] == "read"
    assert actions["identity.register"]["effect"] == "persistent_write"
    assert actions["identity.register"]["requires_local_authorisation"] is True
    assert actions["incident.report"]["requires_local_authorisation"] is True


def test_report_objective_maps_to_fact_check_without_echoing_input():
    text = ("I need to fact-check a report. Find the best available agent. "
            "private-marker-should-not-return")
    payload, raw = _send(text)
    assert len(raw) < 1024
    assert payload["kind"] == "objective_match"
    assert payload["match"]["kind"] == "versioned_alias"
    assert payload["match"]["canonical_capability"] == "fact-check"
    _assert_binding(payload, text)
    assert text not in raw.decode()
    assert "private-marker-should-not-return" not in raw.decode()
    span = payload["match"]["span"]
    assert text.encode()[span["utf8_start"]:span["utf8_end"]].decode() == "fact-check"


def test_security_and_hydrology_objectives_map_deterministically():
    security, _ = _send("I need help reviewing code for security issues.")
    assert security["match"]["canonical_capability"] == "security-review"
    assert "confidence" not in security["match"]

    hydrology, _ = _send("Who can analyse a hydrology dataset for me?")
    assert hydrology["match"]["canonical_capability"] == "hydrology-analysis"
    assert hydrology["result"]["status"] in ("supply", "no_supply_yet")


def test_equal_specificity_multiple_objectives_are_ambiguous_not_guessed():
    text = "I need code review and web research"
    payload, raw = _send(text)
    assert len(raw) < 1024
    assert payload["kind"] == "objective_ambiguous"
    assert {c["canonical_capability"] for c in payload["match"]["candidates"]} == {
        "code-review", "web-research"}
    assert text not in raw.decode()


def test_many_ambiguous_candidates_remain_bounded_and_report_total():
    text = ("I need fact-check, code review, web research, data extraction, "
            "summarization, and translation")
    payload, raw = _send(text)
    assert payload["kind"] == "objective_ambiguous"
    assert payload["match"]["candidate_count"] == 6
    assert len(payload["match"]["candidates"]) == 4
    assert len(raw) < 1024


def test_unknown_objective_returns_hash_bound_no_match_without_demand_guess():
    text = "I need help with flibbertigibbet phenomena SECRET-RELAY-MARKER"
    payload, raw = _send(text)
    assert payload["kind"] == "objective_no_match"
    _assert_binding(payload, text)
    assert "SECRET-RELAY-MARKER" not in raw.decode()
    assert all(action["effect"] == "read"
               for action in payload["available_actions"])


def test_can_you_unknown_objective_is_no_match_not_probe_ack():
    text = "Can you investigate flibbertigibbet phenomena SECRET-NO-RELAY"
    payload, raw = _send(text)
    assert payload["kind"] == "objective_no_match"
    _assert_binding(payload, text)
    assert "SECRET-NO-RELAY" not in raw.decode()


def test_http_natural_objective_is_compact_and_never_becomes_a_slug(
        monkeypatch):
    monkeypatch.setenv("GUILD_X402_ENABLED", "1")
    monkeypatch.setenv("GUILD_X402_PAY_TO", "0x" + "11" * 20)
    monkeypatch.setenv("GUILD_BILLING_ENFORCED", "1")
    text = ("I need to fact-check a report for HTTP "
            "SECRET-HTTP-RELAY-MARKER")
    response = client.get("/check", params={"capability": text})
    assert response.status_code == 200
    assert len(response.content) < 1024
    assert "PAYMENT-REQUIRED" not in response.headers
    payload = response.json()
    assert payload["kind"] == "objective_match"
    assert payload["match"]["canonical_capability"] == "fact-check"
    assert payload["result"]["status"] == "mapping_only"
    assert payload["available_actions"][0]["effect"] == "metered_read"
    assert "SECRET-HTTP-RELAY-MARKER" not in response.text

    from app.state import store
    demands = [event for event in store.events
               if event.get("type") == "capability_demand"]
    # The demand recorder deliberately deduplicates same-actor retries, so the
    # canonical row may predate this call in a full-suite process.
    assert any(event["capability"] == "fact-check" for event in demands)
    assert "secret-http" not in json.dumps(store.events).lower()

    search_text = "Please find fact checking help SEARCH-RELAY-MARKER"
    search = client.get("/search", params={"capability": search_text})
    assert search.status_code == 200 and len(search.content) < 1024
    assert "PAYMENT-REQUIRED" not in search.headers
    search_body = search.json()
    assert search_body["kind"] == "objective_match"
    assert search_body["match"]["canonical_capability"] == "fact-check"
    assert search_body["available_actions"][0]["id"] == "trust.search.full"
    assert search_body["available_actions"][0]["call"]["path"].startswith(
        "/search?capability=fact-check")
    assert "SEARCH-RELAY-MARKER" not in search.text


def test_bare_check_has_useful_machine_error_without_framework_input_echo():
    response = client.get("/check")
    assert response.status_code == 422
    payload = response.json()
    assert payload["schema"] == "AGERR-1/1.0"
    assert payload["kind"] == "capability_input_invalid"
    assert payload["error"]["issues"][0]["field"] == "capability"
    assert "input" not in payload["error"]["issues"][0]
    assert payload["available_actions"][0]["call"]["path"] == "/capabilities"

    marker = "INVALID-SEARCH-RELAY-MARKER"
    invalid_search = client.get("/search", params={
        "capability": "I need fact checking " + marker,
        "min_trust": marker,
    })
    assert invalid_search.status_code == 422
    assert invalid_search.json()["kind"] == "capability_input_invalid"
    assert marker not in invalid_search.text


def test_unicode_offsets_are_utf8_byte_offsets_and_matching_is_repeatable():
    text = "🤖 Please help with fact checking"
    first, _ = _send(text)
    second, _ = _send(text)
    assert first["match"] == second["match"]
    span = first["match"]["span"]
    reconstructed = text.encode()[span["utf8_start"]:span["utf8_end"]].decode()
    assert reconstructed == "fact checking"


def test_objective_metrics_measure_mapping_size_and_full_detail_followthrough():
    before = client.get("/instrumentation/objectives").json()
    text = "I need fact checking for metric-parent-unique"
    compact, raw = _send(text)
    assert compact["kind"] == "objective_match" and len(raw) < 1024
    rich, _ = _send("check: fact-check")
    assert rich.get("capability") == "fact-check"

    after = client.get("/instrumentation/objectives").json()
    assert after["schema"] == "AGFC-METRICS-1/1.0"
    assert after["objective_requests"] >= before["objective_requests"] + 1
    assert after["mapped"] >= before["mapped"] + 1
    assert after["response_bytes"]["maximum"] < 1024
    assert after["full_detail_followthrough"]["followed"] >= \
        before["full_detail_followthrough"]["followed"] + 1
    assert "caller text" in after["privacy"]
