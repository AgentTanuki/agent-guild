"""CENTRAL ANALYTICS INVARIANT (2026-07-10).

No event whose caller class is AG_INTERNAL, AG_TEST, OPERATOR or
REGISTRY_CRAWLER may contribute to genuine_external metrics — at any funnel
stage. is_genuine_external derives from caller_class, and every
genuine_external aggregate in store.instrumentation() filters through
is_genuine_external, so this suite locks the invariant for current metrics
AND historical aggregation (classification is computed at read time).
"""
import os, tempfile

os.environ.setdefault("GUILD_DATA", os.path.join(tempfile.mkdtemp(), "g.json"))

from app.store import Store
from app.attribution import (is_genuine_external, caller_class,
                             may_count_as_external_growth, CALLER_CLASSES)


def _fresh():
    # NB: Store(path="") would share a ".events.jsonl" journal in the cwd
    # across instances — use a unique temp path per store.
    import uuid
    return Store(path=os.path.join(tempfile.mkdtemp(), f"{uuid.uuid4().hex}.json"))


FUNNEL_STAGES = [
    # (event_type, extra_meta) — engagement, invocation, registration,
    # referral, transaction
    ("query", {"caller_kind": "capability_ask", "capability": "fact-check"}),
    ("swarm_invoke", {"capability": "json.repair"}),
    ("registration", {}),
    ("referral", {}),
    ("query", {"paid": True}),
    ("delegation", {"worker_id": "agent_x", "followed": True}),
]

EXCLUDED = {
    "AG_INTERNAL": dict(ua="python-httpx/0.28", fp=True),
    "AG_TEST": dict(ua="ColdDiscoveryHarness/1.0"),
    "OPERATOR": dict(ua="", op=True),
    "REGISTRY_CRAWLER": dict(ua="Glama-Crawler/1.0"),
}


def test_every_excluded_class_is_blocked_at_every_stage():
    for cls_name, attrs in EXCLUDED.items():
        for etype, meta in FUNNEL_STAGES:
            e = {"key": "anon", "type": etype, "at": "2026-07-10T10:00:00+00:00",
                 "ua": attrs.get("ua", ""), "fp": attrs.get("fp", False),
                 **({"op": True} if attrs.get("op") else {}), **meta}
            assert caller_class(e) == cls_name, (cls_name, e, caller_class(e))
            assert not may_count_as_external_growth(caller_class(e))
            assert not is_genuine_external(e), (cls_name, etype)


def test_excluded_classes_cannot_move_store_level_external_metrics():
    """End-to-end: pump excluded-class events through a real store and assert
    the genuine_external funnel stays at zero across engagement, first/repeat
    invocation, registration, referral and paid-transaction counters."""
    s = _fresh()
    for cls_name, attrs in EXCLUDED.items():
        if cls_name == "AG_INTERNAL":
            continue   # fp derives from the account — exercised below
        for etype, meta in FUNNEL_STAGES:
            kwargs = dict(meta)
            if attrs.get("op"):
                kwargs["op"] = True
            s.record_event(None, etype, ua=attrs.get("ua", ""), **kwargs)
    # fp events need a first-party account key to be tagged fp by the store —
    # simulate via explicit fp meta instead (record_event derives fp from the
    # account, so give it one)
    acct = s.create_account(first_party=True)
    for etype, meta in FUNNEL_STAGES:
        s.record_event(acct["key"], etype, ua="python-httpx/0.28", **meta)
    inst = s.instrumentation()
    g = inst["genuine_external"]
    assert inst["genuine_external_detected"] is False
    assert inst["genuine_external_events"] == 0
    assert g["unique_agents"] == 0 and g["first_query"] == 0
    assert g["repeat_query"] == 0 and g["paid_query"] == 0
    assert g["delegations"] == 0
    assert inst["genuine_external_engaged_detected"] is False
    assert inst["genuine_external_engaged_strong_actors"] == []


def test_external_unknown_counts_at_the_correct_stages():
    s = _fresh()
    ua = "python-httpx/0.28"   # framework UA, no fp, no test/crawler marker
    s.record_event("actor-1", "query", ua=ua,
                   caller_kind="capability_ask", capability="fact-check")
    inst = s.instrumentation()
    assert inst["genuine_external_detected"] is True
    assert inst["genuine_external"]["first_query"] == 1
    assert inst["genuine_external"]["repeat_query"] == 0
    # second deciding query by the same actor -> repeat, not first
    s.record_event("actor-1", "query", ua=ua,
                   caller_kind="capability_ask", capability="fact-check")
    inst = s.instrumentation()
    assert inst["genuine_external"]["first_query"] == 1
    assert inst["genuine_external"]["repeat_query"] == 1


def test_external_member_and_verified_classify_and_count():
    s = _fresh()
    a = s.register_agent(name="RealExternal", capabilities=["x"], metadata={})
    e = {"key": a["api_key"], "type": "query", "ua": "python-httpx/0.28",
         "fp": False, "at": "2026-07-10T10:00:00+00:00"}
    assert caller_class(e, member=True) == "EXTERNAL_MEMBER"
    assert caller_class(e, member=True, verified=True) == "EXTERNAL_VERIFIED"
    assert may_count_as_external_growth(caller_class(e, member=True))
    assert is_genuine_external(e)


def test_operator_kill_switch_event_is_audit_only():
    s = _fresh()
    from app.swarm import gateway
    gateway.set_killed(s, True, "invariant drill")
    gateway.set_killed(s, False)
    evs = [e for e in s.events if e["type"].startswith("kill_switch")]
    assert len(evs) == 2
    for e in evs:
        assert e.get("op") is True
        assert caller_class(e) == "OPERATOR"
        assert not is_genuine_external(e)
    inst = s.instrumentation()
    assert inst["genuine_external_events"] == 0


def test_taxonomy_is_closed():
    assert set(CALLER_CLASSES) == {
        "AG_INTERNAL", "AG_TEST", "REGISTRY_CRAWLER", "EXTERNAL_UNKNOWN",
        "EXTERNAL_VERIFIED", "EXTERNAL_MEMBER", "OPERATOR"}
