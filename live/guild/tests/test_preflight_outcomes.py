"""AGPO-1 — the one complete evidence → action → outcome path.

Revised after the 2026-09-22 independent review (Codex), which found that the
first cut could (1) credit AG for a problem the agent found on its own,
(2) treat a User-Agent as operator independence, (3) count a repeated report
twice, and (4) read any success/failure against any verdict. Each of those is
now a counterexample below and must stay red-if-broken.

Also pinned: a skip without a direct check is never a benefit; runs without
outcomes are UNKNOWN; first-party / tooling / propagation records never reach
the headline; the MCP tool and the HTTP route write the same record.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402
from fastmcp import Client  # noqa: E402

from app import abuse  # noqa: E402
from app import preflight  # noqa: E402
from app import preflight_outcomes as po  # noqa: E402
from app import main as main_module  # noqa: E402
from app import mcp_server as mcp_module  # noqa: E402
from app.main import app  # noqa: E402
from app.mcp_server import mcp as guild_mcp  # noqa: E402
from app.store import Store  # noqa: E402

client = TestClient(app)

HEURISTIC_UA = "python-httpxish/1.0"   # framework-like: software identity only
TOOLING_UA = "curl/8.0"
PROPAGATION_UA = "pi-agent-guild/0.1.0"

CHECKS_OK = ["endpoint_reachable", "protocol_handshake", "agent_card_resolves",
             "agent_card_signed", "payment_claim_holds", "independent_evidence"]

# name → (verdict, failed, unknowns)
FIXTURES = {
    "/ok":   ("no_failed_checks", [], ["independent_evidence"]),
    "/down": ("do_not_delegate", ["protocol_handshake"], ["independent_evidence"]),
    "/id":   ("delegate_with_caution", ["agent_card_signed"], ["independent_evidence"]),
    "/pay":  ("do_not_delegate", ["payment_claim_holds"], []),
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    abuse.reset()
    fresh = Store(path=str(tmp_path / "guild.json"))
    monkeypatch.setattr(main_module, "store", fresh)
    monkeypatch.setattr(mcp_module, "store", fresh)

    def fake_run(url, *, store=None):
        suffix = "/" + url.rstrip("/").rsplit("/", 1)[-1]
        verdict, failed, unknowns = FIXTURES.get(suffix, FIXTURES["/ok"])
        checks = [{"check": c, "status": ("failed" if c in failed else
                                           "unknown" if c in unknowns else "proven"),
                   "detail": "d"} for c in CHECKS_OK]
        return {"target": url, "verdict": verdict, "headline": "x",
                "checks": checks, "failed": list(failed),
                "unknowns": list(unknowns),
                "scored": [c for c in CHECKS_OK if c not in unknowns],
                "method": "m", "limits": "l"}
    monkeypatch.setattr(preflight, "run", fake_run)
    yield fresh


def _register(ua: str) -> str:
    r = client.post("/agents/register", json={"name": "eval-participant",
                                        "capabilities": ["code-review"]},
                    headers={"User-Agent": ua})
    assert r.status_code in (200, 201), r.text
    body = r.json()
    return body["api_key"]


def _pf(url: str, ua: str, key: str | None = None) -> dict:
    h = {"User-Agent": ua}
    if key:
        h["X-API-Key"] = key
    r = client.get("/preflight", params={"url": url}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def _report(body: dict, ua: str, key: str | None = None):
    h = {"User-Agent": ua}
    if key:
        h["X-API-Key"] = key
    return client.post("/preflight/outcome", json=body, headers=h)


def _summary() -> dict:
    r = client.get("/preflight/outcomes")
    assert r.status_code == 200
    return r.json()


def _bucket(name: str) -> dict:
    return _summary()["by_ownership"][name]


# --- the verdict carries the identity an outcome must cite ------------------

def test_preflight_is_stamped_and_the_run_event_keeps_check_names(_isolate):
    out = _pf("https://x.example/down", HEURISTIC_UA)
    assert out["preflight_id"].startswith("pf_")
    assert out["observed_at"].endswith("+00:00")
    assert isinstance(out["probe_latency_ms"], int)
    act = out["outcome_report"]
    assert act["call"] == {"method": "POST", "path": "/preflight/outcome"}
    assert "decision_basis?" in act["body"]
    runs = [e for e in _isolate.events if e["type"] == "preflight_run"]
    assert runs[-1]["preflight_id"] == out["preflight_id"]
    assert runs[-1]["failed_checks"] == ["protocol_handshake"]
    assert runs[-1]["unknown_checks"] == ["independent_evidence"]


def test_summary_is_empty_and_honest_before_any_report():
    s = _summary()
    assert s["schema"] == "AGPO-1/1.1"
    assert s["independently_demonstrated_benefit"] is False
    assert s["headline"] == "No independently demonstrated benefit yet."
    assert s["rules"] == list(po.RULES)
    assert set(s["by_ownership"]) == set(po.OWNERSHIPS)


# --- review finding 1: AG is only credited when AG was right AND drove it ---

def test_benefit_requires_warning_verdict_direct_failure_and_ag_basis():
    key = _register(HEURISTIC_UA)
    out = _pf("https://x.example/down", HEURISTIC_UA, key)
    r = _report({"preflight_id": out["preflight_id"], "action": "declined",
                 "baseline": {"direct_check": "failure"},
                 "decision_basis": "ag_evidence",
                 "reporter_kind": "integration"}, HEURISTIC_UA, key)
    assert r.status_code == 201, r.text
    b = _bucket("registered_participant")
    assert b["benefit"] == 1 and b["by_result"] == {"avoided_broken_endpoint": 1}
    assert _summary()["independently_demonstrated_benefit"] is True


def test_ag_right_but_agent_decided_on_its_own_check_is_not_a_benefit():
    key = _register(HEURISTIC_UA)
    out = _pf("https://x.example/down", HEURISTIC_UA, key)
    _report({"preflight_id": out["preflight_id"], "action": "declined",
             "baseline": {"direct_check": "failure"},
             "decision_basis": "own_check", "reporter_kind": "agent"},
            HEURISTIC_UA, key)
    b = _bucket("registered_participant")
    assert b["benefit"] == 0
    assert b["by_result"] == {"correct_warning_independently_confirmed": 1}
    assert _summary()["independently_demonstrated_benefit"] is False


def test_ag_clean_verdict_but_direct_check_failed_is_a_missed_problem():
    # The counterexample from the review: AG unhelpful, agent found the
    # problem itself. That is AG's mistake, not AG's benefit.
    key = _register(HEURISTIC_UA)
    out = _pf("https://x.example/ok", HEURISTIC_UA, key)
    _report({"preflight_id": out["preflight_id"], "action": "skipped",
             "baseline": {"direct_check": "failure"},
             "decision_basis": "own_check", "reporter_kind": "agent"},
            HEURISTIC_UA, key)
    b = _bucket("registered_participant")
    assert b["mistake"] == 1 and b["benefit"] == 0
    assert b["by_result"] == {"missed_problem": 1}


def test_influence_not_reported_stays_unresolved():
    key = _register(HEURISTIC_UA)
    out = _pf("https://x.example/down", HEURISTIC_UA, key)
    _report({"preflight_id": out["preflight_id"], "action": "declined",
             "baseline": {"direct_check": "failure"},
             "reporter_kind": "agent"}, HEURISTIC_UA, key)
    b = _bucket("registered_participant")
    assert b["unresolved"] == 1 and b["benefit"] == 0
    assert b["by_result"] == {"correct_warning_influence_unknown": 1}


def test_skip_without_a_direct_check_is_never_a_benefit():
    key = _register(HEURISTIC_UA)
    out = _pf("https://x.example/down", HEURISTIC_UA, key)
    _report({"preflight_id": out["preflight_id"], "action": "skipped",
             "decision_basis": "ag_evidence", "reporter_kind": "agent"},
            HEURISTIC_UA, key)
    b = _bucket("registered_participant")
    assert b["by_result"] == {"counterfactual_unobserved": 1}
    assert b["benefit"] == 0 and b["unknown"] == 1


# --- review finding 4: outcomes are matched to the verdict's scope ----------

def test_success_does_not_contradict_an_identity_warning():
    key = _register(HEURISTIC_UA)
    out = _pf("https://x.example/id", HEURISTIC_UA, key)   # caution: card unsigned
    _report({"preflight_id": out["preflight_id"], "action": "called",
             "observed": "success", "detail": "ok", "reporter_kind": "agent"},
            HEURISTIC_UA, key)
    b = _bucket("registered_participant")
    assert b["mistake"] == 0
    assert b["by_result"] == {"warning_not_contradicted": 1}


def test_success_does_contradict_a_reachability_warning():
    key = _register(HEURISTIC_UA)
    out = _pf("https://x.example/down", HEURISTIC_UA, key)  # handshake failed
    _report({"preflight_id": out["preflight_id"], "action": "called",
             "observed": "success", "detail": "ok", "reporter_kind": "agent"},
            HEURISTIC_UA, key)
    b = _bucket("registered_participant")
    assert b["by_result"] == {"unnecessary_refusal": 1} and b["mistake"] == 1


def test_failure_outside_the_verdicts_scope_is_not_a_missed_problem():
    key = _register(HEURISTIC_UA)
    out = _pf("https://x.example/ok", HEURISTIC_UA, key)
    _report({"preflight_id": out["preflight_id"], "action": "called",
             "observed": "failure", "detail": "wrong_result",
             "reporter_kind": "agent"}, HEURISTIC_UA, key)
    b = _bucket("registered_participant")
    assert b["mistake"] == 0 and b["unresolved"] == 1
    assert b["by_result"] == {"failure_out_of_scope": 1}


def test_failure_in_a_check_ag_said_passed_is_a_missed_problem():
    key = _register(HEURISTIC_UA)
    out = _pf("https://x.example/ok", HEURISTIC_UA, key)
    _report({"preflight_id": out["preflight_id"], "action": "delegated",
             "observed": "failure", "detail": "protocol_error",
             "reporter_kind": "integration"}, HEURISTIC_UA, key)
    assert _bucket("registered_participant")["by_result"] == {"missed_problem": 1}


def test_failure_in_a_check_ag_marked_unknown_is_not_a_missed_problem():
    key = _register(HEURISTIC_UA)
    out = _pf("https://x.example/pay", HEURISTIC_UA, key)  # independent_evidence not unknown here
    # use /ok, whose unknown check is independent_evidence — no detail maps to it,
    # so exercise the classifier directly for the unknown branch:
    run = {"verdict": "no_failed_checks", "failed_checks": [],
           "unknown_checks": ["payment_claim_holds"],
           "scored_checks": ["endpoint_reachable"]}
    c = po.classify(run, {"action": "called", "observed": "failure",
                          "detail": "payment_mismatch"})
    assert c == {"result": "unknown_check_not_covered", "kind": "neutral"}
    # and the warned-about failure, when it happens, is evidence_correct_not_followed
    _report({"preflight_id": out["preflight_id"], "action": "called",
             "observed": "failure", "detail": "payment_mismatch",
             "reporter_kind": "agent"}, HEURISTIC_UA, key)
    assert _bucket("registered_participant")["by_result"] == {"evidence_correct_not_followed": 1}


def test_success_after_a_clean_verdict_is_consistency_not_benefit():
    key = _register(HEURISTIC_UA)
    out = _pf("https://x.example/ok", HEURISTIC_UA, key)
    _report({"preflight_id": out["preflight_id"], "action": "called",
             "observed": "success", "reporter_kind": "integration",
             "overhead_ms": 40}, HEURISTIC_UA, key)
    b = _bucket("registered_participant")
    assert b["by_result"] == {"consistent_outcome": 1}
    assert b["benefit"] == 0 and b["neutral"] == 1
    assert b["median_reported_overhead_ms"] == 40


# --- review finding 3: one decision per preflight_id --------------------------

def test_repeated_identical_reports_count_one_decision():
    key = _register(HEURISTIC_UA)
    out = _pf("https://x.example/down", HEURISTIC_UA, key)
    body = {"preflight_id": out["preflight_id"], "action": "declined",
            "baseline": {"direct_check": "failure"},
            "decision_basis": "ag_evidence", "reporter_kind": "integration"}
    assert _report(body, HEURISTIC_UA, key).status_code == 201
    assert _report(body, HEURISTIC_UA, key).status_code == 201
    b = _bucket("registered_participant")
    assert b["decisions"] == 1 and b["benefit"] == 1
    assert b["report_events"] == 2 and b["corrected_decisions"] == 0


def test_a_later_different_report_is_a_visible_correction():
    key = _register(HEURISTIC_UA)
    out = _pf("https://x.example/down", HEURISTIC_UA, key)
    first = {"preflight_id": out["preflight_id"], "action": "declined",
             "baseline": {"direct_check": "failure"},
             "decision_basis": "ag_evidence", "reporter_kind": "integration"}
    _report(first, HEURISTIC_UA, key)
    _report({**first, "decision_basis": "own_check"}, HEURISTIC_UA, key)
    b = _bucket("registered_participant")
    assert b["decisions"] == 1 and b["corrected_decisions"] == 1
    assert b["benefit"] == 0
    assert b["by_result"] == {"correct_warning_independently_confirmed": 1}


def test_stale_reports_do_not_join_as_decisions():
    # Backend-agnostic: feed summary() a synthetic durable snapshot.
    class FakeStore:
        def measurement_event_snapshot(self, **_):
            run = {"type": "preflight_run", "preflight_id": "pf_" + "c" * 20,
                   "at": "2026-01-01T00:00:00+00:00", "ua": HEURISTIC_UA,
                   "verdict": "do_not_delegate",
                   "failed_checks": ["protocol_handshake"], "unknown_checks": [],
                   "scored_checks": ["endpoint_reachable", "protocol_handshake"],
                   "participant_agent_id": "agent_x", "fp": False}
            rep = {"type": "preflight_outcome", "preflight_id": "pf_" + "c" * 20,
                   "at": "2026-01-03T00:00:00+00:00", "ua": HEURISTIC_UA,
                   "action": "declined", "observed": "unknown",
                   "detail": "not_applicable", "baseline_direct_check": "failure",
                   "decision_basis": "ag_evidence", "reporter_kind": "integration",
                   "participant_agent_id": "agent_x", "fp": False}
            return [run, rep], {"source": "fake", "history_complete": True}
    s = po.summary(FakeStore())
    b = s["by_ownership"]["registered_participant"]
    assert b["by_result"] == {"stale_join": 1} and b["benefit"] == 0
    assert s["independently_demonstrated_benefit"] is False


# --- review finding 2: ownership is not software identity -------------------

def test_framework_user_agent_is_heuristic_external_never_headline():
    out = _pf("https://x.example/down", HEURISTIC_UA)
    _report({"preflight_id": out["preflight_id"], "action": "declined",
             "baseline": {"direct_check": "failure"},
             "decision_basis": "ag_evidence", "reporter_kind": "integration"},
            HEURISTIC_UA)
    s = _summary()
    assert s["by_ownership"]["heuristic_external"]["benefit"] == 1
    assert s["by_ownership"]["registered_participant"]["decisions"] == 0
    assert s["independently_demonstrated_benefit"] is False


def test_propagation_and_tooling_never_reach_the_headline():
    out = _pf("https://x.example/down", PROPAGATION_UA)
    _report({"preflight_id": out["preflight_id"], "action": "declined",
             "baseline": {"direct_check": "failure"},
             "decision_basis": "ag_evidence", "reporter_kind": "integration"},
            PROPAGATION_UA)
    out2 = _pf("https://y.example/down", TOOLING_UA)
    _report({"preflight_id": out2["preflight_id"], "action": "declined",
             "baseline": {"direct_check": "failure"},
             "decision_basis": "ag_evidence", "reporter_kind": "integration"},
            TOOLING_UA)
    s = _summary()
    assert s["by_ownership"]["propagation_client"]["benefit"] == 1
    assert s["by_ownership"]["tooling_or_crawler"]["benefit"] == 1
    assert s["by_ownership"]["registered_participant"]["decisions"] == 0
    assert s["independently_demonstrated_benefit"] is False


def test_registered_participant_requires_the_same_key_on_both_legs():
    k1, k2 = _register(HEURISTIC_UA), _register(HEURISTIC_UA)
    out = _pf("https://x.example/down", HEURISTIC_UA, k1)
    _report({"preflight_id": out["preflight_id"], "action": "declined",
             "baseline": {"direct_check": "failure"},
             "decision_basis": "ag_evidence", "reporter_kind": "integration"},
            HEURISTIC_UA, k2)
    s = _summary()
    assert s["by_ownership"]["registered_participant"]["decisions"] == 0
    assert s["independently_demonstrated_benefit"] is False
    # key on the report only
    out2 = _pf("https://y.example/down", HEURISTIC_UA)
    _report({"preflight_id": out2["preflight_id"], "action": "declined",
             "baseline": {"direct_check": "failure"},
             "decision_basis": "ag_evidence", "reporter_kind": "integration"},
            HEURISTIC_UA, k1)
    assert _summary()["by_ownership"]["registered_participant"]["decisions"] == 0


def test_pi_extension_with_a_participant_key_is_a_registered_participant():
    # The selected route to independent use: an operator registers once and
    # the extension presents that key on both legs.
    key = _register(PROPAGATION_UA)
    out = _pf("https://x.example/down", PROPAGATION_UA, key)
    _report({"preflight_id": out["preflight_id"], "action": "declined",
             "baseline": {"direct_check": "failure"},
             "decision_basis": "ag_evidence", "reporter_kind": "integration"},
            PROPAGATION_UA, key)
    b = _bucket("registered_participant")
    assert b["decisions"] == 1 and b["benefit"] == 1
    assert b["by_client_type"] == {"ag_distributed_client": 1}


def test_first_party_key_is_first_party_whatever_the_user_agent(_isolate):
    key = _register(HEURISTIC_UA)
    agent = _isolate.agent_for_presented_key(key)
    with _isolate.lock:
        _isolate.agents[agent["id"]]["first_party"] = True
    out = _pf("https://x.example/down", HEURISTIC_UA, key)
    _report({"preflight_id": out["preflight_id"], "action": "declined",
             "baseline": {"direct_check": "failure"},
             "decision_basis": "ag_evidence", "reporter_kind": "integration"},
            HEURISTIC_UA, key)
    s = _summary()
    assert s["by_ownership"]["first_party"]["decisions"] == 1
    assert s["by_ownership"]["registered_participant"]["decisions"] == 0


def test_invalid_key_is_refused_not_downgraded():
    r = client.get("/preflight", params={"url": "https://x.example/ok"},
                   headers={"User-Agent": HEURISTIC_UA, "X-API-Key": "nope"})
    assert r.status_code == 401
    r = _report({"preflight_id": "pf_" + "a" * 20, "action": "called",
                 "observed": "success", "reporter_kind": "agent"},
                HEURISTIC_UA, "nope")
    assert r.status_code == 401


# --- unknowns stay unknown ----------------------------------------------------

def test_runs_without_outcomes_are_unknown_not_refusal():
    for i in range(3):
        _pf(f"https://x{i}.example/ok", HEURISTIC_UA)
    b = _bucket("heuristic_external")
    assert b["runs_stamped"] == 3 and b["runs_without_outcome"] == 3
    assert b["decisions"] == 0


def test_outcome_for_unknown_id_never_joins():
    r = _report({"preflight_id": "pf_" + "a" * 20, "action": "called",
                 "observed": "success", "reporter_kind": "agent"}, HEURISTIC_UA)
    assert r.status_code == 201
    s = _summary()
    assert s["unjoined_outcome_reports"] == 1
    assert all(b["decisions"] == 0 for b in s["by_ownership"].values())


# --- validation refuses what would corrupt the ledger ------------------------

@pytest.mark.parametrize("body,needle", [
    ({"preflight_id": "nope", "action": "called", "reporter_kind": "agent"}, "preflight_id"),
    ({"preflight_id": "pf_" + "b" * 20, "action": "paid", "reporter_kind": "agent"}, "action"),
    ({"preflight_id": "pf_" + "b" * 20, "action": "declined", "observed": "success",
      "reporter_kind": "agent"}, "observed"),
    ({"preflight_id": "pf_" + "b" * 20, "action": "called", "observed": "success",
      "reporter_kind": "human"}, "reporter_kind"),
    ({"preflight_id": "pf_" + "b" * 20, "action": "called", "observed": "success",
      "reporter_kind": "agent", "decision_basis": "vibes"}, "decision_basis"),
    ({"preflight_id": "pf_" + "b" * 20, "action": "called", "observed": "success",
      "reporter_kind": "agent", "baseline": {"direct_check": "maybe"}}, "direct_check"),
])
def test_invalid_reports_get_a_machine_capsule(body, needle, _isolate):
    r = _report(body, HEURISTIC_UA)
    assert r.status_code == 422
    j = r.json()
    assert j["schema"] == "AGERR-1/1.0" and j["kind"] == "preflight_outcome_invalid"
    assert needle in j["error"]["detail"]
    assert all(e["type"] != "preflight_outcome" for e in _isolate.events)


def test_outcome_reports_are_rate_limited(monkeypatch):
    monkeypatch.setenv("GUILD_ABUSE_CONTROLS", "1")
    monkeypatch.setenv("GUILD_RL_PREFLIGHT_OUTCOME", "2")
    abuse.reset()
    out = _pf("https://x.example/ok", HEURISTIC_UA)
    body = {"preflight_id": out["preflight_id"], "action": "called",
            "observed": "success", "reporter_kind": "agent"}
    assert _report(body, HEURISTIC_UA).status_code == 201
    assert _report(body, HEURISTIC_UA).status_code == 201
    assert _report(body, HEURISTIC_UA).status_code == 429


# --- MCP parity ---------------------------------------------------------------

def _data(res):
    d = res.data if hasattr(res, "data") else res
    if isinstance(d, list):
        d = json.loads(d[0].text)
    return d


def test_mcp_tool_writes_the_same_record(_isolate):
    async def go():
        async with Client(guild_mcp) as c:
            pf = _data(await c.call_tool("guild_preflight",
                                         {"url": "https://x.example/down"}))
            assert pf["preflight_id"].startswith("pf_")
            rd = _data(await c.call_tool("guild_preflight_outcome", {
                "preflight_id": pf["preflight_id"], "action": "declined",
                "baseline_direct_check": "failure",
                "decision_basis": "ag_evidence"}))
            assert rd["recorded"] is True
    asyncio.run(go())
    outcomes = [e for e in _isolate.events if e["type"] == "preflight_outcome"]
    assert len(outcomes) == 1
    assert outcomes[0]["transport"] == "mcp"
    assert outcomes[0]["reporter_kind"] == "agent"
    assert outcomes[0]["decision_basis"] == "ag_evidence"
    # MCP carries no participant key, so this can never be the headline.
    s = _summary()
    assert s["by_ownership"]["registered_participant"]["decisions"] == 0
    assert s["independently_demonstrated_benefit"] is False
