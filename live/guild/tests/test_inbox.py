"""guild_inbox (app/inbox.py): the Guild's per-agent in-band message channel.

Locks: subject-only free read (never metered, never third-party readable);
internal-only writes (admin token, or first-party token in STRICT mode only
— honor mode must refuse, or the inbox is an open spam channel); in-band
delivery rides guild_next exactly once per message; automatic idempotent
liveness-expiry warnings (warn ≤7 days out, dedupe per expiry timestamp,
re-armed by a refresh); message expiry + per-agent bound; inbox never
creates evidence, standing or liveness.
"""
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, store  # noqa: E402
from app.store import Store  # noqa: E402
from app import inbox, journey  # noqa: E402

client = TestClient(app)


def _iso(dt):
    return dt.isoformat()


def _now():
    return datetime.now(timezone.utc)


def _register(name="inbox-agent"):
    r = client.post("/agents/register",
                    json={"name": name, "capabilities": ["fact-check"]})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("GUILD_FIRST_PARTY_TOKEN", raising=False)
    monkeypatch.delenv("GUILD_FIRST_PARTY_TOKEN_PREV", raising=False)
    yield


# --- queue + read authorization -------------------------------------------

def test_inbox_read_is_subject_only_and_free():
    reg = _register("inbox-read")
    other = _register("inbox-other")
    aid = reg["id"]
    inbox.queue_message(store, aid, topic="hello", body="first message")

    r = client.get(f"/agents/{aid}/inbox",
                   headers={"X-Api-Key": reg["api_key"]})
    assert r.status_code == 200
    assert r.headers["X-Guild-Cost"] == "0"
    data = r.json()
    assert data["count"] == 1
    assert data["messages"][0]["topic"] == "hello"

    # a third party's valid key is refused — inbox is private correspondence
    r = client.get(f"/agents/{aid}/inbox",
                   headers={"X-Api-Key": other["api_key"]})
    assert r.status_code == 403
    # so is an anonymous read
    assert client.get(f"/agents/{aid}/inbox").status_code == 403


def test_inbox_write_requires_admin_or_strict_first_party(monkeypatch):
    reg = _register("inbox-write")
    aid = reg["id"]
    payload = {"topic": "op", "body": "operator note"}

    # honor mode (no token configured): ANY header value must be refused
    r = client.post(f"/agents/{aid}/inbox", json=payload,
                    headers={"X-Agent-Guild-First-Party": "anything"})
    assert r.status_code == 403

    # strict mode + the right token → queued
    monkeypatch.setenv("GUILD_FIRST_PARTY_TOKEN", "fp-tok")
    r = client.post(f"/agents/{aid}/inbox", json=payload,
                    headers={"X-Agent-Guild-First-Party": "fp-tok"})
    assert r.status_code == 200 and r.json()["queued"] is True
    # strict mode + a wrong token → refused
    r = client.post(f"/agents/{aid}/inbox", json=payload,
                    headers={"X-Agent-Guild-First-Party": "wrong"})
    assert r.status_code == 403

    # admin token works independently
    monkeypatch.setattr("app.main.ADMIN_TOKEN", "adm")
    r = client.post(f"/agents/{aid}/inbox",
                    json={"topic": "op2", "body": "b"},
                    headers={"X-Admin-Token": "adm"})
    assert r.status_code == 200 and r.json()["queued"] is True

    # the subject's own api key grants NO write access
    r = client.post(f"/agents/{aid}/inbox", json=payload,
                    headers={"X-Api-Key": reg["api_key"]})
    assert r.status_code == 403


def test_dedupe_key_makes_queueing_idempotent():
    s = Store(path="")
    a = s.register_agent("dedupe", ["cap"], {})
    m1 = inbox.queue_message(s, a["id"], topic="t", body="b",
                             dedupe_key="k1")
    assert m1 is not None
    assert inbox.queue_message(s, a["id"], topic="t", body="b",
                               dedupe_key="k1") is None
    assert len(s.guild_inbox[a["id"]]) == 1


# --- in-band delivery on guild_next ---------------------------------------

def test_guild_next_delivers_each_message_exactly_once():
    s = Store(path="")
    a = s.register_agent("deliver", ["cap"], {})
    inbox.queue_message(s, a["id"], topic="t1", body="first")

    block = journey.guild_next(s, a)
    assert [m["topic"] for m in block["inbox"]["messages"]] == ["t1"]
    assert any(e["type"] == "inbox_delivered" for e in s.events)

    # delivered → does not ride again…
    assert "inbox" not in journey.guild_next(s, a)
    # …but stays visible in the full self-read (delivery never consumes)
    assert inbox.inbox_view(s, a)["count"] == 1


def test_expired_messages_are_dropped():
    s = Store(path="")
    a = s.register_agent("expire", ["cap"], {})
    m = inbox.queue_message(s, a["id"], topic="old", body="stale")
    m["expires_at"] = _iso(_now() - timedelta(seconds=1))
    assert inbox.inbox_view(s, a)["count"] == 0
    assert "inbox" not in journey.guild_next(s, a)


def test_per_agent_bound_drops_oldest():
    s = Store(path="")
    a = s.register_agent("bound", ["cap"], {})
    for i in range(inbox.MAX_MESSAGES_PER_AGENT + 5):
        inbox.queue_message(s, a["id"], topic=f"t{i}", body="b")
    box = s.guild_inbox[a["id"]]
    assert len(box) == inbox.MAX_MESSAGES_PER_AGENT
    assert box[0]["topic"] == "t5"          # oldest five dropped


# --- automatic liveness-expiry warnings -----------------------------------

def _proof(expires_in_days):
    return {"verified_at": _iso(_now()),
            "liveness_expires_at": _iso(_now() +
                                        timedelta(days=expires_in_days))}


def test_liveness_warning_queued_when_expiry_is_near():
    s = Store(path="")
    a = s.register_agent("live-soon", ["cap"], {})
    a["proof_of_conduct"] = _proof(3)       # inside the 7-day window
    block = journey.guild_next(s, a)
    msgs = block["inbox"]["messages"]
    assert [m["topic"] for m in msgs] == ["liveness_expiry"]
    assert msgs[0]["action"]["action"] == "refresh_liveness"
    assert f"/agents/{a['id']}/prove" in msgs[0]["action"]["call"]
    # idempotent: a second pass queues nothing new
    journey.guild_next(s, a)
    assert sum(1 for m in s.guild_inbox[a["id"]]
               if m["topic"] == "liveness_expiry") == 1


def test_liveness_warning_not_queued_when_fresh_and_rearms_on_refresh():
    s = Store(path="")
    a = s.register_agent("live-fresh", ["cap"], {})
    a["proof_of_conduct"] = _proof(13)      # outside the window
    inbox.ensure_liveness_warning(s, a)
    assert not s.guild_inbox.get(a["id"])
    # expiry approaches → warns (also for ALREADY-expired proofs)
    a["proof_of_conduct"] = _proof(-1)
    inbox.ensure_liveness_warning(s, a)
    assert len(s.guild_inbox[a["id"]]) == 1
    # a refresh moves the expiry → the NEW timestamp re-arms the dedupe
    a["proof_of_conduct"] = _proof(2)
    inbox.ensure_liveness_warning(s, a)
    assert len(s.guild_inbox[a["id"]]) == 2
    keys = {m["dedupe_key"] for m in s.guild_inbox[a["id"]]}
    assert len(keys) == 2


def test_inbox_never_touches_evidence_or_liveness():
    s = Store(path="")
    a = s.register_agent("no-sideeffects", ["cap"], {})
    a["proof_of_conduct"] = _proof(-1)      # expired
    before = dict(a["proof_of_conduct"])
    journey.guild_next(s, a)                # delivers the warning
    assert a["proof_of_conduct"] == before  # nothing refreshed by transport
    assert not s.attestations
    assert all(e["type"].startswith("inbox_") or "inbox" not in e["type"]
               for e in s.events)
