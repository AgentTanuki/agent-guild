"""AGPO-1 — the one complete evidence → action → outcome path.

These tests pin the honesty properties, not the happy path:
  * a verdict carries a preflight_id an outcome must cite; unknown ids never join;
  * benefits AND mistakes are classified by the published rule table;
  * a skip without a direct check is counterfactual_unobserved, never a benefit;
  * runs without outcomes are UNKNOWN, never refusal;
  * first-party / tooling / propagation records can never satisfy the
    independent objective — only EXTERNAL_* callers on both legs can;
  * the MCP tool and the HTTP route write the same record.
"""
from __future__ import annotations

import asyncio
import os

import pytest

os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402
from fastmcp import Client  # noqa: E402

from app import abuse  # noqa: E402
from app import preflight  # noqa: E402
from app import preflight_outcomes as po  # noqa: E402
from app.main import app  # noqa: E402
from app.mcp_server import mcp as guild_mcp  # noqa: E402
from app.state import store  # noqa: E402

client = TestClient(app)

INDEPENDENT_UA = "python-httpxish/1.0"   # framework-like, not tooling, not ours
TOOLING_UA = "curl/8.0"
PROPAGATION_UA = "pi-agent-guild/0.1.0"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    abuse.reset()
    with store.lock:
        store.events.clear()
    # Never touch the network: a fixed verdict per URL suffix.
    def fake_run(url, *, store=None):
        verdict = ("do_not_delegate" if url.endswith("/bad")
                   else "delegate_with_caution" if url.endswith("/meh")
                   else "no_failed_checks")
        return {"target": url, "verdict": verdict, "headline": "x",
                "checks": [{"check": "reachable", "status": "proven",
                            "detail": "d"}],
                "failed": [] if verdict == "no_failed_checks" else ["reachable"],
                "unknowns": [], "scored": ["reachable"],
                "method": "m", "limits": "l"}
    monkeypatch.setattr(preflight, "run", fake_run)
    yield


def _pf(url: str, ua: str) -> dict:
    r = client.get("/preflight", params={"url": url},
                   headers={"User-Agent": ua})
    assert r.status_code == 200, r.text
    return r.json()


def _report(body: dict, ua: str):
    return client.post("/preflight/outcome", json=body,
                       headers={"User-Agent": ua})


def _summary() -> dict:
    r = client.get("/preflight/outcomes")
    assert r.status_code == 200
    return r.json()


# --- the verdict now carries the identity an outcome must cite -------------

def test_preflight_is_stamped_with_id_time_latency_and_report_action():
    out = _pf("https://x.example/mcp", INDEPENDENT_UA)
    assert out["preflight_id"].startswith("pf_")
    assert out["observed_at"].endswith("+00:00")
    assert isinstance(out["probe_latency_ms"], int)
    assert out["freshness"]["kind"] == "live_probe"
    act = out["outcome_report"]
    assert act["call"] == {"method": "POST", "path": "/preflight/outcome"}
    assert act["requires_local_authorisation"] is False
    assert act["body"]["preflight_id"] == out["preflight_id"]
    # and the run event carries the same id so the join is exact
    runs = [e for e in store.events if e["type"] == "preflight_run"]
    assert runs[-1]["preflight_id"] == out["preflight_id"]


def test_summary_is_empty_and_honest_before_any_report():
    s = _summary()
    assert s["schema"] == "AGPO-1/1.0"
    assert s["independently_demonstrated_benefit"] is False
    assert s["headline"] == "No independently demonstrated benefit yet."
    assert s["rules"] == list(po.RULES)
    assert set(s["by_actor_class"]) == {"independent", "first_party",
                                        "propagation_client",
                                        "tooling_or_crawler"}


# --- the rule table, exercised through the real path ------------------------

def test_skip_with_verified_direct_failure_is_the_only_way_to_a_benefit():
    out = _pf("https://x.example/bad", INDEPENDENT_UA)
    r = _report({"preflight_id": out["preflight_id"], "action": "declined",
                 "baseline": {"direct_check": "failure"},
                 "reporter_kind": "integration"}, INDEPENDENT_UA)
    assert r.status_code == 201, r.text
    ind = _summary()["by_actor_class"]["independent"]
    assert ind["benefit"] == 1 and ind["mistake"] == 0
    assert ind["by_result"] == {"avoided_broken_endpoint": 1}
    assert ind["by_evidence_label"]["observed_action"] == 1
    assert _summary()["independently_demonstrated_benefit"] is True


def test_skip_without_a_direct_check_is_never_a_benefit():
    out = _pf("https://x.example/bad", INDEPENDENT_UA)
    r = _report({"preflight_id": out["preflight_id"], "action": "skipped",
                 "reporter_kind": "agent"}, INDEPENDENT_UA)
    assert r.status_code == 201
    ind = _summary()["by_actor_class"]["independent"]
    assert ind["benefit"] == 0
    assert ind["by_result"] == {"counterfactual_unobserved": 1}
    assert ind["unknown"] == 1
    assert _summary()["independently_demonstrated_benefit"] is False


def test_mistakes_are_measured_with_equal_weight():
    # AG said do_not_delegate; the agent called anyway and it worked.
    bad = _pf("https://x.example/bad", INDEPENDENT_UA)
    assert _report({"preflight_id": bad["preflight_id"], "action": "called",
                    "observed": "success", "detail": "ok",
                    "reporter_kind": "integration"},
                   INDEPENDENT_UA).status_code == 201
    # AG said no_failed_checks; the call failed.
    ok = _pf("https://x.example/ok", INDEPENDENT_UA)
    assert _report({"preflight_id": ok["preflight_id"], "action": "delegated",
                    "observed": "failure", "detail": "http_5xx",
                    "reporter_kind": "agent"}, INDEPENDENT_UA).status_code == 201
    ind = _summary()["by_actor_class"]["independent"]
    assert ind["mistake"] == 2 and ind["benefit"] == 0
    assert ind["by_result"] == {"unnecessary_refusal": 1, "missed_problem": 1}
    assert ind["by_verdict"] == {"do_not_delegate": 1, "no_failed_checks": 1}


def test_success_after_a_clean_verdict_is_no_difference_not_benefit():
    ok = _pf("https://x.example/ok", INDEPENDENT_UA)
    _report({"preflight_id": ok["preflight_id"], "action": "called",
             "observed": "success", "reporter_kind": "integration",
             "overhead_ms": 40}, INDEPENDENT_UA)
    ind = _summary()["by_actor_class"]["independent"]
    assert ind["by_result"] == {"no_difference": 1}
    assert ind["neutral"] == 1 and ind["benefit"] == 0
    assert ind["median_reported_overhead_ms"] == 40


def test_failure_after_a_warning_is_evidence_correct_but_no_benefit():
    bad = _pf("https://x.example/bad", INDEPENDENT_UA)
    _report({"preflight_id": bad["preflight_id"], "action": "called",
             "observed": "failure", "detail": "unreachable",
             "reporter_kind": "agent"}, INDEPENDENT_UA)
    ind = _summary()["by_actor_class"]["independent"]
    assert ind["by_result"] == {"evidence_correct_not_followed": 1}
    assert ind["benefit"] == 0


# --- unknowns stay unknown -------------------------------------------------

def test_runs_without_outcomes_are_unknown_not_refusal():
    for i in range(3):
        _pf(f"https://x{i}.example/ok", INDEPENDENT_UA)
    s = _summary()
    ind = s["by_actor_class"]["independent"]
    assert ind["runs_stamped"] == 3 and ind["runs_without_outcome"] == 3
    assert ind["outcomes"] == 0
    assert "UNKNOWN" in s["reading_rules"]["runs_without_outcome"]


def test_outcome_for_unknown_id_never_joins():
    r = _report({"preflight_id": "pf_" + "a" * 20, "action": "called",
                 "observed": "success", "reporter_kind": "agent"},
                INDEPENDENT_UA)
    assert r.status_code == 201
    s = _summary()
    assert s["unjoined_outcome_reports"] == 1
    assert all(b["outcomes"] == 0 for b in s["by_actor_class"].values())


# --- independence is structural -------------------------------------------

def test_tooling_and_propagation_records_never_count_as_independent():
    out = _pf("https://x.example/bad", TOOLING_UA)
    _report({"preflight_id": out["preflight_id"], "action": "declined",
             "baseline": {"direct_check": "failure"},
             "reporter_kind": "integration"}, TOOLING_UA)
    out2 = _pf("https://y.example/bad", PROPAGATION_UA)
    _report({"preflight_id": out2["preflight_id"], "action": "declined",
             "baseline": {"direct_check": "failure"},
             "reporter_kind": "integration"}, PROPAGATION_UA)
    s = _summary()
    assert s["by_actor_class"]["tooling_or_crawler"]["benefit"] == 1
    assert s["by_actor_class"]["propagation_client"]["benefit"] == 1
    assert s["by_actor_class"]["independent"]["outcomes"] == 0
    assert s["independently_demonstrated_benefit"] is False


def test_independence_requires_both_legs_to_be_external():
    # Evidence requested by an independent UA, outcome reported by bare tooling:
    # the stricter class wins, so this is NOT independent.
    out = _pf("https://x.example/bad", INDEPENDENT_UA)
    _report({"preflight_id": out["preflight_id"], "action": "declined",
             "baseline": {"direct_check": "failure"},
             "reporter_kind": "integration"}, TOOLING_UA)
    s = _summary()
    assert s["by_actor_class"]["independent"]["outcomes"] == 0
    assert s["by_actor_class"]["tooling_or_crawler"]["outcomes"] == 1


# --- validation refuses what would corrupt the ledger ----------------------

@pytest.mark.parametrize("body,needle", [
    ({"preflight_id": "nope", "action": "called", "reporter_kind": "agent"},
     "preflight_id"),
    ({"preflight_id": "pf_" + "b" * 20, "action": "paid", "reporter_kind": "agent"},
     "action"),
    ({"preflight_id": "pf_" + "b" * 20, "action": "declined",
      "observed": "success", "reporter_kind": "agent"}, "observed"),
    ({"preflight_id": "pf_" + "b" * 20, "action": "called",
      "observed": "success", "reporter_kind": "human"}, "reporter_kind"),
    ({"preflight_id": "pf_" + "b" * 20, "action": "called",
      "observed": "success", "reporter_kind": "agent",
      "baseline": {"direct_check": "maybe"}}, "direct_check"),
])
def test_invalid_reports_get_a_machine_capsule_without_echo(body, needle):
    r = _report(body, INDEPENDENT_UA)
    assert r.status_code == 422
    j = r.json()
    assert j["schema"] == "AGERR-1/1.0"
    assert j["kind"] == "preflight_outcome_invalid"
    assert needle in j["error"]["detail"]
    assert "nope" not in r.text and "paid" not in j["error"]["detail"] or needle == "action"
    assert store.events == [] or all(e["type"] != "preflight_outcome"
                                     for e in store.events)


def test_outcome_reports_are_rate_limited(monkeypatch):
    monkeypatch.setenv("GUILD_ABUSE_CONTROLS", "1")
    monkeypatch.setenv("GUILD_RL_PREFLIGHT_OUTCOME", "2")
    abuse.reset()
    out = _pf("https://x.example/ok", INDEPENDENT_UA)
    body = {"preflight_id": out["preflight_id"], "action": "called",
            "observed": "success", "reporter_kind": "agent"}
    assert _report(body, INDEPENDENT_UA).status_code == 201
    assert _report(body, INDEPENDENT_UA).status_code == 201
    assert _report(body, INDEPENDENT_UA).status_code == 429


# --- MCP parity --------------------------------------------------------------

def test_mcp_tool_writes_the_same_record():
    async def go():
        async with Client(guild_mcp) as c:
            pf = await c.call_tool("guild_preflight",
                                   {"url": "https://x.example/bad"})
            data = pf.data if hasattr(pf, "data") else pf
            if isinstance(data, list):
                import json
                data = json.loads(data[0].text)
            assert data["preflight_id"].startswith("pf_")
            res = await c.call_tool("guild_preflight_outcome", {
                "preflight_id": data["preflight_id"], "action": "declined",
                "baseline_direct_check": "failure"})
            rd = res.data if hasattr(res, "data") else res
            if isinstance(rd, list):
                import json
                rd = json.loads(rd[0].text)
            assert rd["recorded"] is True
    asyncio.run(go())
    outcomes = [e for e in store.events if e["type"] == "preflight_outcome"]
    assert len(outcomes) == 1
    assert outcomes[0]["transport"] == "mcp"
    assert outcomes[0]["reporter_kind"] == "agent"
    assert outcomes[0]["baseline_direct_check"] == "failure"
    # The in-process fastmcp client advertises its own clientInfo, which the
    # existing attribution rule treats as a named MCP client we do not operate.
    # That is a limit of UA-based attribution, recorded here on purpose: a
    # first-party test can look independent unless it is marked first-party.
    s = _summary()
    assert s["by_actor_class"]["independent"]["outcomes"] + \
        s["by_actor_class"]["tooling_or_crawler"]["outcomes"] == 1
