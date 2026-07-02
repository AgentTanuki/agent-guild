"""Stage-2 dual-write: the collaboration ledger is durable and the hash chain
persists across restarts.

Locks the constraint this sprint removed: recorded collaborations must survive a
process restart (persisted to disk) and continue the same hash chain — so the
ledger accumulates over time instead of being recomputed from a projection.
"""
import os
import tempfile

from app.store import Store
from app.ledger import Ledger


def _fresh_path():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)  # Store creates it on first save
    return path


def _agents(s):
    a = s.register_agent("Req", ["hiring"], metadata={})
    b = s.register_agent("Wkr", ["summarize"], metadata={})
    return a, b


def test_records_persist_across_restart():
    path = _fresh_path()
    try:
        s1 = Store(path=path)
        a, b = _agents(s1)
        s1.record_collaboration(a, b["id"], "summarize", "accepted", 0.95,
                                deliverable="a good summary")
        # stage-1: the chain carries typed events too (register, receipt,
        # attestation) — exactly one COLLABORATION record.
        assert s1.durable_ledger().stats()["collaborations"] == 1
        n1 = len(s1.ledger_records)
        head1 = s1.ledger_records[-1]["hash"]

        # "restart": a brand-new Store loading the same file
        s2 = Store(path=path)
        assert len(s2.ledger_records) == n1
        assert s2.ledger_records[-1]["hash"] == head1
        assert s2.durable_ledger().verify_chain() is True
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_chain_continues_across_appends_and_stays_valid():
    path = _fresh_path()
    try:
        s = Store(path=path)
        a, b = _agents(s)
        c = s.register_agent("Wkr2", ["research"], metadata={})
        r1 = s.record_collaboration(a, b["id"], "summarize", "accepted", 0.9,
                                    deliverable="one")
        r2 = s.record_collaboration(a, c["id"], "research", "accepted", 0.8,
                                    deliverable="two")
        # one chain, strictly increasing seq, later collab sealed after earlier
        assert r2["ledger_record"]["seq"] > r1["ledger_record"]["seq"]
        seqs = [d["seq"] for d in s.ledger_records]
        assert seqs == list(range(len(seqs)))
        assert s.durable_ledger().verify_chain() is True
        assert s.durable_ledger().stats()["collaborations"] == 2
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_backfill_heals_missing_collab_records_idempotently():
    path = _fresh_path()
    try:
        s = Store(path=path)
        a, b = _agents(s)
        # multi-call path: a graded, content-addressed task with attestation
        t = s.create_task(a["id"], b["id"], "summarize", payment=0.0)
        s.submit_receipt(t["id"], "0xabc123", outcome="accepted")
        s.add_custodial_attestation(a, b, "summarize", 0.9, t["id"], "", stake=0.0)
        # the healing backfill (run inside durable_ledger) captures the graded
        # task as a collab record automatically, and is idempotent
        assert s.durable_ledger().stats()["collaborations"] == 1
        s.ensure_ledger_backfilled()
        assert s.durable_ledger().stats()["collaborations"] == 1   # idempotent
        assert s.durable_ledger().verify_chain() is True

        # legacy-file simulation: a store whose chain predates dual-write
        # (tasks exist, chain empty) is healed by the startup backfill.
        s.ledger_records = []
        s.ensure_ledger_backfilled()
        assert s.durable_ledger().stats()["collaborations"] == 1
        assert s.durable_ledger().verify_chain() is True
    finally:
        if os.path.exists(path):
            os.remove(path)
