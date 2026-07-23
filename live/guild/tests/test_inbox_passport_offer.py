"""The one-time inbox passport offer (passport programme 2026-07-23).

ensure_passport_offer mirrors ensure_liveness_warning: a proved agent
(prove_completed) that has never fetched its passport (no first_passport)
gets ONE machine-readable inbox message with the exact claim/verify/badge/
expose instructions, delivered in-band on the same warm channel as every
other Guild message. Once per counterparty FOREVER: queue_message's
dedupe_key only spans live messages, so the durable passport_offer_sent
milestone guards re-queueing even after the message's TTL expires.
"""
import os

os.environ.setdefault("GUILD_DATA", "")

from datetime import datetime, timedelta, timezone  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.state import store  # noqa: E402
from app import inbox  # noqa: E402

client = TestClient(app)


def _register(name):
    r = client.post("/agents/register",
                    json={"name": name, "capabilities": ["x"]})
    assert r.status_code == 200
    return r.json()


def _prove(reg):
    h = {"X-API-Key": reg["api_key"]}
    assert client.post(f"/agents/{reg['id']}/prove",
                       headers=h).status_code == 200
    r = client.post(f"/agents/{reg['id']}/prove/verify", headers=h)
    assert r.status_code == 200
    return r.json()


def _offer_msgs(aid):
    return [m for m in store.guild_inbox.get(aid, [])
            if m.get("dedupe_key") == f"passport_offer:{aid}"]


def test_offer_queued_once_and_delivered_in_band_on_prove():
    reg = _register("InboxOffer1")
    out = _prove(reg)
    # queued by the very delivery pass the prove response triggers, and
    # delivered in-band on that same warm response
    msgs = _offer_msgs(reg["id"])
    assert len(msgs) == 1
    inbox_blk = out["guild_next"].get("inbox") or {}
    topics = [m["topic"] for m in inbox_blk.get("messages", [])]
    assert "Claim your Agent Guild passport" in topics
    msg = msgs[0]
    assert msg["source"] == "guild_system"
    assert msg["action"] == {"method": "GET",
                             "url": f"https://agent-guild-5d5r.onrender.com"
                                    f"/agents/{reg['id']}/passport"}
    for needle in ("/passport", "/credentials/verify", "badge.svg",
                   "agent-guild-did.json", "agent card, manifest"):
        assert needle in msg["body"], needle
    # the durable guard stamped exactly once
    assert "passport_offer_sent" in store.get_agent(reg["id"])["milestones"]


def test_offer_is_idempotent_across_delivery_passes():
    reg = _register("InboxOffer2")
    _prove(reg)
    for _ in range(3):
        r = client.get(f"/agents/{reg['id']}/inbox",
                       headers={"X-API-Key": reg["api_key"]})
        assert r.status_code == 200
    assert len(_offer_msgs(reg["id"])) == 1


def test_offer_never_requeued_after_expiry():
    """Once per counterparty FOREVER: even after the live message expires (so
    queue_message's live-message dedupe can no longer see it), the durable
    milestone guard refuses a second offer."""
    reg = _register("InboxOffer3")
    _prove(reg)
    aid = reg["id"]
    msgs = _offer_msgs(aid)
    assert len(msgs) == 1
    # force TTL expiry; the next read prunes the dead message
    msgs[0]["expires_at"] = (datetime.now(timezone.utc)
                             - timedelta(seconds=1)).isoformat()
    r = client.get(f"/agents/{aid}/inbox",
                   headers={"X-API-Key": reg["api_key"]})
    assert r.status_code == 200
    assert "Claim your Agent Guild passport" not in \
        [m["topic"] for m in r.json()["messages"]]
    # live-dedupe alone would re-queue here; the milestone must not
    agent = store.get_agent(aid)
    inbox.ensure_passport_offer(store, agent)
    assert _offer_msgs(aid) == []


def test_no_offer_for_unproven_or_already_passported_agents():
    reg = _register("InboxOffer4")
    # unproven: nothing to offer
    r = client.get(f"/agents/{reg['id']}/inbox",
                   headers={"X-API-Key": reg["api_key"]})
    assert _offer_msgs(reg["id"]) == []
    # proved but ALREADY holding first_passport: the offer is spent
    reg2 = _register("InboxOffer5")
    h = {"X-API-Key": reg2["api_key"]}
    client.post(f"/agents/{reg2['id']}/prove", headers=h)
    # fetch the passport first, through the store, before any delivery pass
    now = datetime.now(timezone.utc).isoformat()
    agent2 = store.get_agent(reg2["id"])
    agent2.setdefault("milestones", {})["prove_completed"] = now
    agent2["milestones"]["first_passport"] = now
    inbox.ensure_passport_offer(store, agent2)
    assert _offer_msgs(reg2["id"]) == []
