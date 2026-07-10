"""Pilot A instrumentation audit (2026-07-10): explicit caller classes.

Internal activity must be structurally unable to generate external-growth
metrics: crawlers, our own tests, and first-party traffic are separated by
TYPE, and only EXTERNAL_* classes pass may_count_as_external_growth.
"""
import os, tempfile

os.environ.setdefault("GUILD_DATA", os.path.join(tempfile.mkdtemp(), "g.json"))

from app.attribution import (caller_class, may_count_as_external_growth,
                             CALLER_CLASSES, is_genuine_external)
from fastapi.testclient import TestClient
from app.main import app, store

client = TestClient(app)


def _e(ua="", fp=False, at="2026-07-10T10:00:00+00:00", key=None):
    return {"ua": ua, "fp": fp, "at": at, "key": key}


def test_first_party_is_ag_internal():
    assert caller_class(_e(ua="python-httpx/0.28", fp=True)) == "AG_INTERNAL"


def test_admin_is_operator():
    assert caller_class(_e(), operator=True) == "OPERATOR"


def test_known_test_harness_ua_is_ag_test():
    assert caller_class(_e(ua="ColdDiscoveryHarness/1.0 (deterministic)")) == "AG_TEST"
    assert caller_class(_e(ua="guild-ops-check/2")) == "AG_TEST"


def test_known_first_party_incident_is_ag_test():
    e = _e(ua="mcp:probe/1", at="2026-07-10T09:00:00+00:00")
    assert caller_class(e) == "AG_TEST"
    # ...and is therefore no longer genuine external either
    assert not is_genuine_external(e)
    # same UA OUTSIDE the incident window still counts as external
    late = _e(ua="mcp:probe/1", at="2026-07-11T09:00:00+00:00")
    assert caller_class(late) == "EXTERNAL_UNKNOWN"


def test_registry_crawlers_are_never_external():
    for ua in ("Glama-Crawler/1.0", "Smithery/scan", "UptimeRobot/2.0",
               "Mozilla/5.0 (compatible; bingbot/2.0)", "kube-probe/1.27"):
        cls = caller_class(_e(ua=ua))
        assert cls == "REGISTRY_CRAWLER", ua
        assert not may_count_as_external_growth(cls)


def test_member_and_verified_classes():
    e = _e(ua="python-httpx/0.28", key="sk_abc")
    assert caller_class(e, member=True) == "EXTERNAL_MEMBER"
    assert caller_class(e, member=True, verified=True) == "EXTERNAL_VERIFIED"
    assert caller_class(e) == "EXTERNAL_UNKNOWN"


def test_growth_gate_is_closed_by_type():
    external = {c for c in CALLER_CLASSES if may_count_as_external_growth(c)}
    assert external == {"EXTERNAL_UNKNOWN", "EXTERNAL_VERIFIED", "EXTERNAL_MEMBER"}


def test_instrumentation_exposes_caller_class_counts():
    r = client.post("/agents/register",
                    json={"name": "CCTest", "capabilities": ["x"]},
                    headers={"X-Guild-Source": "test"})
    assert r.status_code == 200
    inst = client.get("/instrumentation").json()
    assert "caller_classes" in inst
    assert set(inst["caller_classes"]) <= set(CALLER_CLASSES)
    assert sum(inst["caller_classes"].values()) >= 1


def test_ag_test_and_crawler_uas_are_never_genuine_external():
    """Found live 2026-07-10: the MCP verification battery (mcp:pilot-a-audit)
    was AG_TEST under caller_class yet still counted in the genuine_external
    headline, because is_genuine_external never consulted the AG-test/crawler
    UA rules. The two classifiers must agree at the growth gate."""
    from app.attribution import attribution_class
    for ua in ("mcp:pilot-a-audit/1", "ColdDiscoveryHarness/1.0",
               "Glama-Crawler/1.0", "UptimeRobot/2.0"):
        e = _e(ua=ua)
        assert not is_genuine_external(e), ua
        assert attribution_class(e) in ("ag_test", "registry_crawler"), (
            ua, attribution_class(e))
