"""Stage 1 prep: the durable chain carries ALL evidence events, typed.

Locks four guarantees:
  1. Every evidence-bearing mutation dual-writes a typed entry (register,
     config_change, receipt, attestation, escrow_event) onto the SAME hash
     chain as collaboration records — one chain, no restart.
  2. Mixed chains are tamper-evident end to end, and legacy (collab-only)
     chains still verify byte-for-byte.
  3. Reputation derivation is unchanged by typed events (they are raw
     evidence for the interpretation layer, not scored records yet).
  4. Chain entries never contain secrets (no private keys, no api keys).
"""
import json
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from app.store import Store  # noqa: E402
from app.ledger import (  # noqa: E402
    Ledger, CollaborationRecord, GenericEntry, GENERIC_ENTRY_TYPES,
    entry_from_dict,
)

CFG = {"model": "m1", "constitution_hash": "c1", "tools": []}
CFG2 = {"model": "m2", "constitution_hash": "c1", "tools": []}


def _store_with_activity():
    s = Store(path="")
    a = s.register_agent("Req", ["hiring"], metadata={}, config=CFG,
                         principal="org:test")
    b = s.register_agent("Wkr", ["summarize"], metadata={}, config=CFG)
    s.declare_configuration(b["id"], CFG2)
    t = s.create_task(a["id"], b["id"], "summarize", payment=2.0)
    s.submit_receipt(t["id"], "0xhash", outcome="accepted")
    s.add_custodial_attestation(a, b, "summarize", 0.9, t["id"], "", stake=1.0)
    return s, a, b


def _types(s):
    return [d.get("type", "collab") for d in s.ledger_records]


# --- 1. dual-write coverage ---------------------------------------------------

def test_every_evidence_mutation_lands_on_the_chain():
    s, a, b = _store_with_activity()
    types = _types(s)
    assert types.count("register") == 2
    assert types.count("config_change") == 1
    assert types.count("receipt") == 1
    assert types.count("attestation") == 1
    assert s.durable_ledger().verify_chain() is True


def test_escrow_lifecycle_lands_on_the_chain():
    s, a, b = _store_with_activity()
    acct = s.create_account(owner_agent_id=a["id"])
    s.credit(acct["key"], 1000, reason="test")
    esc = s.open_escrow(acct["key"], b["id"], 100, capability="summarize")
    s.release_escrow(esc["id"], acct["key"], deliverable="done", rating=0.9)
    events = [d["body"]["event"] for d in s.ledger_records
              if d.get("type") == "escrow_event"]
    assert events == ["opened", "released"]
    # settlement produced a guild_mediated collaboration on the same chain
    assert s.durable_ledger().stats()["collaborations"] >= 1
    assert s.durable_ledger().verify_chain() is True


def test_escrow_dispute_lands_on_the_chain():
    s, a, b = _store_with_activity()
    acct = s.create_account(owner_agent_id=a["id"])
    s.credit(acct["key"], 1000, reason="test")
    esc = s.open_escrow(acct["key"], b["id"], 100)
    s.dispute_escrow(esc["id"], acct["key"], grounds="not delivered")
    disputes = [d for d in s.ledger_records if d.get("type") == "escrow_event"
                and d["body"]["event"] == "disputed"]
    assert len(disputes) == 1
    assert disputes[0]["body"]["grounds"] == "not delivered"


# --- 2. integrity: mixed + legacy ---------------------------------------------

def test_mixed_chain_is_tamper_evident():
    s, _, _ = _store_with_activity()
    led = Ledger.from_records(s.ledger_records)
    assert led.verify_chain() is True
    # tamper with a typed entry's body
    for r in led.records:
        if isinstance(r, GenericEntry) and r.type == "register":
            r.body["name"] = "Someone Else"
            break
    assert led.verify_chain() is False


def test_seq_and_linkage_are_one_unbroken_chain():
    s, _, _ = _store_with_activity()
    prev = "0" * 64
    for i, d in enumerate(s.ledger_records):
        assert d["seq"] == i
        assert d["prev_hash"] == prev
        assert entry_from_dict(d).recompute_hash() == d["hash"]
        prev = d["hash"]


def test_legacy_collab_only_chain_still_verifies():
    """Chains persisted before typed entries existed must verify unchanged."""
    s = Store(path="")
    a = s.register_agent("Req", ["hiring"], metadata={})
    b = s.register_agent("Wkr", ["summarize"], metadata={})
    s.record_collaboration(a, b["id"], "summarize", "accepted", 0.9,
                           deliverable="one")
    legacy = [d for d in s.ledger_records if "type" not in d]
    # re-link the collab-only records as their own chain (as a legacy file was)
    relinked = Ledger()
    for d in legacy:
        rec = {k: v for k, v in d.items()}
        rec.pop("hash"), rec.pop("id")
        rec.pop("seq"), rec.pop("prev_hash")
        relinked.append(CollaborationRecord(seq=0, prev_hash="", hash="", id="", **rec))
    assert relinked.verify_chain() is True


# --- 3. reputation unchanged by typed events ----------------------------------

def test_typed_events_do_not_move_ledger_reputation():
    s, a, b = _store_with_activity()
    s.record_collaboration(a, b["id"], "summarize", "accepted", 0.95,
                           deliverable="real work", payment=5.0)
    led = Ledger.from_records(s.ledger_records)
    rep = led.derive_reputation()
    # derivation sees only collabs: worker appears, register/receipt noise doesn't
    assert b["id"] in rep
    assert rep[b["id"]]["records"] == led.stats()["collaborations"]


# --- 4. no secrets on the chain -------------------------------------------------

def test_chain_contains_no_secrets():
    s, a, b = _store_with_activity()
    blob = json.dumps(s.ledger_records)
    assert a["api_key"] not in blob
    assert b["api_key"] not in blob
    for agent in s.agents.values():
        if agent.get("private_key"):
            assert agent["private_key"] not in blob


def test_generic_entry_types_are_closed_set():
    s = Store(path="")
    try:
        s.append_ledger_event("not_a_type", {})
        assert False, "unknown entry types must be rejected"
    except ValueError:
        pass
    assert set(GENERIC_ENTRY_TYPES) == {
        "register", "config_change", "receipt", "attestation", "escrow_event",
        "task_created", "reclassification", "issuer_rotation"}
