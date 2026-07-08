"""Per-caller attribution for anonymous A2A traffic (2026-07-08).

The muddiness this locks against: every inbound A2A message used to be recorded
against the single literal actor key ``"a2a"``, so a real external decider and a
polling/probing process collapsed into one bucket and
``genuine_external_engaged_detected`` could not tell them apart. On top of that,
every message unconditionally emits a guild-side ``prove_surfaced`` reply against
the caller's key, and the old rule miscounted that as engagement — so ANY
genuine poller tripped the "a real agent engaged" headline.

These tests prove:
  * two anonymous A2A callers no longer collapse into one ``"a2a"`` bucket;
  * bare probes are counted in ``genuine_external_probe_only_events``;
  * real follow-up/engagement is counted in ``genuine_external_engaged_detected``;
  * mixed probe + engaged callers do not contaminate each other;
  * guild-side reply events (prove_surfaced) are never counted as engagement.
"""
import json
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.state import store  # noqa: E402
from app.store import Store  # noqa: E402
from app.attribution import (  # noqa: E402
    derive_a2a_actor, engagement_kind, is_bare_probe, is_strong_deciding,
)

client = TestClient(app)

_FRAMEWORK_UA = "python-httpx/0.28.1"  # genuine-external (agent framework) UA


def _send(text, headers=None):
    r = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": "message/send",
              "params": {"message": {"parts": [{"kind": "text", "text": text}]}}},
        headers=headers or {},
    )
    assert r.status_code == 200
    return json.loads(r.json()["result"]["parts"][0]["text"])


def _actor_for(**headers):
    """The actor key the endpoint would derive for a set of request headers."""
    return derive_a2a_actor(headers, headers.get("x-forwarded-for", ""), "")


# ---------------------------------------------------------------------------
# derive_a2a_actor: strongest-signal-wins, namespaced, secrets fingerprinted
# ---------------------------------------------------------------------------

def test_derive_never_returns_plain_a2a_and_is_always_namespaced():
    for hdrs in ({}, {"user-agent": "x"}, {"x-forwarded-for": "9.9.9.9"},
                 {"x-api-key": "sk_secret"}, {"x-agent-id": "orch-7"}):
        key = derive_a2a_actor(hdrs, hdrs.get("x-forwarded-for", ""), "")
        assert key != "a2a"
        assert key.startswith("a2a:")


def test_two_anonymous_callers_do_not_collapse():
    """Different network fingerprints → different actor keys (the core bug)."""
    a = derive_a2a_actor({"user-agent": _FRAMEWORK_UA}, "1.1.1.1", "")
    b = derive_a2a_actor({"user-agent": _FRAMEWORK_UA}, "2.2.2.2", "")
    assert a != b
    # ...and the SAME fingerprint is stable (a monitor keeps one bucket).
    a2 = derive_a2a_actor({"user-agent": _FRAMEWORK_UA}, "1.1.1.1", "")
    assert a == a2


def test_identity_priority_id_beats_token_beats_network():
    hdrs = {"x-agent-id": "orch-9", "x-api-key": "sk_x",
            "user-agent": _FRAMEWORK_UA}
    assert derive_a2a_actor(hdrs, "1.1.1.1", "").startswith("a2a:aid:")
    hdrs2 = {"x-api-key": "sk_x", "user-agent": _FRAMEWORK_UA}
    assert derive_a2a_actor(hdrs2, "1.1.1.1", "").startswith("a2a:key:")
    hdrs3 = {"user-agent": _FRAMEWORK_UA}
    assert derive_a2a_actor(hdrs3, "1.1.1.1", "").startswith("a2a:net:")
    # an agent_id named in the message body is an explicit identity too
    assert derive_a2a_actor({}, "", "hi from agent_deadbeef12").startswith("a2a:aid:")


def test_secrets_and_ips_are_fingerprinted_not_stored_raw():
    """A token or IP must never appear verbatim in the derived key."""
    k = derive_a2a_actor({"x-api-key": "sk_topsecret_value"}, "", "")
    assert "sk_topsecret_value" not in k
    k2 = derive_a2a_actor({"user-agent": _FRAMEWORK_UA}, "203.0.113.7", "")
    assert "203.0.113.7" not in k2


def test_derived_key_can_never_be_spoofed_into_a_real_billing_key():
    """A caller setting x-api-key to a known ak_/sk_ key can't hijack that
    account's attribution: the derived key is hashed and 'a2a:'-namespaced."""
    k = derive_a2a_actor({"x-api-key": "ak_victimaccount"}, "", "")
    assert not k.startswith(("ak_", "sk_"))
    assert k.startswith("a2a:key:")


# ---------------------------------------------------------------------------
# Endpoint wiring: distinct callers land in distinct buckets
# ---------------------------------------------------------------------------

def test_endpoint_records_distinct_actor_keys_for_distinct_callers():
    key_a = _actor_for(**{"user-agent": _FRAMEWORK_UA, "x-forwarded-for": "10.0.0.1"})
    key_b = _actor_for(**{"user-agent": _FRAMEWORK_UA, "x-forwarded-for": "10.0.0.2"})
    assert key_a != key_b and key_a != "a2a" and key_b != "a2a"

    _send("ping", {"user-agent": _FRAMEWORK_UA, "x-forwarded-for": "10.0.0.1"})
    _send("ping", {"user-agent": _FRAMEWORK_UA, "x-forwarded-for": "10.0.0.2"})

    keys = {e["key"] for e in store.events if e.get("endpoint") == "a2a_message"}
    assert key_a in keys and key_b in keys
    assert "a2a" not in keys  # the collapsed literal is gone


# ---------------------------------------------------------------------------
# Engagement classification (fresh Store — isolated from cross-test events)
# ---------------------------------------------------------------------------

def _probe(s, key, ua=_FRAMEWORK_UA):
    s.record_event(key, "query", ua=f"a2a:{ua}", endpoint="a2a_message",
                   text="ping", caller_kind="probe", capability=None)


def _guild_surfacing(s, key, ua=_FRAMEWORK_UA):
    # what the endpoint emits in reply to EVERY message, against the caller key
    s.record_event(key, "prove_surfaced", ua=f"a2a:{ua}", endpoint="a2a_message")


def _capability_ask(s, key, ua=_FRAMEWORK_UA):
    s.record_event(key, "query", ua=f"a2a:{ua}", endpoint="a2a_message",
                   text="check: fact-check", caller_kind="capability_ask",
                   capability="fact-check")


def test_bare_probe_and_guild_reply_classified_correctly():
    probe = {"type": "query", "endpoint": "a2a_message", "caller_kind": "probe"}
    assert is_bare_probe(probe) and engagement_kind(probe) == "probe"
    reply = {"type": "prove_surfaced", "endpoint": "a2a_message"}
    assert engagement_kind(reply) == "guild_surfacing"
    ask = {"type": "query", "endpoint": "a2a_message",
           "caller_kind": "capability_ask", "capability": "fact-check"}
    assert engagement_kind(ask) == "deciding"


def test_pure_poller_is_probe_only_never_engaged():
    """A framework-UA caller that only ever probes (and receives guild replies)
    is genuine external traffic but NOT engagement — the prove_surfaced replies
    must not flip the detector (the contamination this whole fix targets)."""
    s = Store(path="")
    poller = "a2a:net:poller"
    for _ in range(20):
        _probe(s, poller)
        _guild_surfacing(s, poller)   # emitted for every message, like prod
    instr = s.instrumentation()
    assert instr["genuine_external_detected"] is True         # honest: it IS traffic
    assert instr["genuine_external_events"] == 40
    assert instr["genuine_external_engaged_detected"] is False  # ...but NOT engaged
    assert poller in instr["genuine_external_probe_only_actors"]
    assert instr["genuine_external_probe_only_events"] == 20
    # the 20 guild replies are surfaced for audit, counted as neither signal
    assert instr["genuine_external_guild_surfacing_events"] == 20
    assert poller not in instr["genuine_external_engaged"]["actors"]


def test_real_capability_ask_counts_as_engaged():
    s = Store(path="")
    decider = "a2a:net:decider"
    _capability_ask(s, decider)
    _guild_surfacing(s, decider)
    instr = s.instrumentation()
    assert instr["genuine_external_engaged_detected"] is True
    assert decider in instr["genuine_external_engaged"]["actors"]
    assert decider not in instr["genuine_external_probe_only_actors"]


def test_mixed_probe_and_engaged_callers_do_not_contaminate():
    """Distinct fingerprints: a poller (probes only) and a decider (capability
    ask). The poller must stay probe-only; the decider must be engaged; neither
    leaks into the other's bucket."""
    s = Store(path="")
    poller, decider = "a2a:net:poll", "a2a:net:decide"
    for _ in range(10):
        _probe(s, poller)
        _guild_surfacing(s, poller)
    _capability_ask(s, decider)
    _guild_surfacing(s, decider)
    instr = s.instrumentation()

    assert poller in instr["genuine_external_probe_only_actors"]
    assert poller not in instr["genuine_external_engaged"]["actors"]
    assert decider in instr["genuine_external_engaged"]["actors"]
    assert decider not in instr["genuine_external_probe_only_actors"]
    # detection is driven by the decider, not the 10 probes or the guild replies
    assert instr["genuine_external_engaged_detected"] is True
    assert instr["genuine_external_probe_only_events"] == 10


def test_lone_capability_ask_is_deciding_but_not_strong():
    """A single capability-shaped ask is engagement, but an automated monitor
    could emit it — so it is NOT in the higher-confidence strong subset until
    the caller registers/proves/pays or repeats a deciding action."""
    s = Store(path="")
    weak = "a2a:net:weak"
    _capability_ask(s, weak)
    instr = s.instrumentation()
    assert weak in instr["genuine_external_engaged"]["actors"]
    assert weak not in instr["genuine_external_engaged_strong_actors"]
    assert instr["genuine_external_engaged_strong_detected"] is False

    # a second, distinct deciding action promotes it to strong
    _capability_ask(s, weak)
    instr2 = s.instrumentation()
    assert weak in instr2["genuine_external_engaged_strong_actors"]


def test_registration_is_strong_deciding_on_its_own():
    reg = {"type": "register"}
    assert is_strong_deciding(reg) is True
    probe = {"type": "query", "endpoint": "a2a_message", "caller_kind": "probe"}
    assert is_strong_deciding(probe) is False
