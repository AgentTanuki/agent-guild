"""Discovery swarm — capability registry: fixture gate, schemas, safety guards."""
import jsonschema
import pytest

from app.swarm.capabilities import (
    CAPABILITIES, CapabilityError, run_capability, validate_all)


def test_pilot_a_cohort_size():
    # Pilot A: 10–20 verified capabilities
    assert 10 <= len(CAPABILITIES) <= 20


def test_every_capability_has_complete_template():
    for cap in CAPABILITIES.values():
        assert cap.id and cap.version and cap.summary and cap.description
        assert cap.input_schema.get("type") == "object"
        assert cap.output_schema.get("type") == "object"
        assert len(cap.fixtures) >= 1, cap.id
        assert cap.failure_modes and cap.prohibited_uses
        assert cap.safety_class == "deterministic_transform"
        assert cap.demand_hypothesis and cap.baseline
        # schemas must themselves be valid JSON Schema
        jsonschema.Draft202012Validator.check_schema(cap.input_schema)
        jsonschema.Draft202012Validator.check_schema(cap.output_schema)


def test_publish_gate_all_pass():
    results = validate_all()
    failing = {k: v for k, v in results.items() if not v["ok"]}
    assert not failing, f"fixture gate failing: {failing}"


def test_outputs_conform_to_output_schema():
    for cap in CAPABILITIES.values():
        for fx in cap.fixtures:
            if fx.get("expect_error"):
                continue
            out, _ = run_capability(cap.id, fx["input"])
            jsonschema.validate(out, cap.output_schema)


def test_input_schema_rejects_garbage():
    with pytest.raises(jsonschema.ValidationError):
        run_capability("json.repair", {"nope": 1})
    with pytest.raises(jsonschema.ValidationError):
        run_capability("calc.stats", {"values": "not-a-list"})


def test_unknown_capability_raises():
    with pytest.raises(KeyError):
        run_capability("does.not.exist", {})


def test_regex_guard_rejects_catastrophic_pattern():
    with pytest.raises(CapabilityError):
        run_capability("text.regex_extract", {"text": "aaa", "pattern": "(a+)+$"})


def test_json_repair_handles_llm_style_output():
    out, _ = run_capability("json.repair", {
        "text": "```json\n{'name': 'x', \"tags\": ['a', 'b',], // note\n 'ok': True\n```"})
    assert out["ok"] is True
    assert out["parsed"] == {"name": "x", "tags": ["a", "b"], "ok": True}


def test_canonicalize_is_deterministic():
    a, _ = run_capability("json.canonicalize", {"value": {"b": [1, 2], "a": True}})
    b, _ = run_capability("json.canonicalize", {"value": {"a": True, "b": [1, 2]}})
    assert a["sha256"] == b["sha256"]


def test_semver_prerelease_precedence():
    out, _ = run_capability("code.semver_compare",
                            {"a": "1.0.0-alpha.1", "b": "1.0.0-alpha.beta"})
    assert out["comparison"] == -1  # numeric identifiers < alphanumeric


def test_csv_roundtrip_composes():
    csv_out, _ = run_capability("table.json_to_csv",
                                {"rows": [{"x": "1", "y": "a"}, {"x": "2", "y": "b"}]})
    back, _ = run_capability("table.csv_to_json", {"csv": csv_out["csv"]})
    assert back["rows"] == [{"x": "1", "y": "a"}, {"x": "2", "y": "b"}]
