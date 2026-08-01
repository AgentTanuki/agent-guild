"""Divergence incident 2026-07-30/31 — regression suite for the truth layer.

WHAT PRODUCTION ACTUALLY DID
  * ``/instrumentation`` and ``/funnel/passports`` each served the PREVIOUS
    stable snapshot on the first reads of a session, then the current one.
  * ``POST /ledger/checkpoint/publish`` returned checkpoint index 14 /
    ledger_length 834 while the published feed was already at 16 / 836.

WHAT WAS AND WAS NOT PROVED (see docs/DIVERGENCE_2026-07-31.md)
  The read-side flip could not be attributed: 40 concurrent ``/release`` probes
  returned ONE ``_PROCESS_STARTED_AT``, 60 concurrent mixed requests showed no
  cross-request body mixing, and unique cache-busters ruled out URL-keyed
  caching. So the read-side cause remains UNKNOWN and is deliberately NOT
  "fixed" here — it is made DECIDABLE (instance/revision stamping) instead.

  The WRITE side is a different matter: a stale/racing durable view on the
  canonical commitment path is reproducible, and these tests reproduce it with
  two ``Store`` instances over one shared SQLite file — the faithful analogue
  of two writers over one disk. That class of bug is now refused, not papered
  over.
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import instanceid  # noqa: E402
from app.store import (  # noqa: E402
    CanonicalWriteRefused,
    CheckpointForkError,
    CheckpointWriteVerificationError,
    StaleDurableStateError,
    Store,
)


def _sqlite_store(tmp_path, name="guild.json") -> Store:
    os.environ["GUILD_STORE"] = "sqlite"
    os.environ["GUILD_STORE_PATH"] = str(tmp_path / "guild.sqlite3")
    return Store(path=str(tmp_path / name))


@pytest.fixture(autouse=True)
def _clean_env():
    yield
    for k in ("GUILD_STORE", "GUILD_STORE_PATH"):
        os.environ.pop(k, None)


# --------------------------------------------------------------------------
# 1. View identity — the decidability layer
# --------------------------------------------------------------------------
def test_instance_identity_is_non_secret_and_stable_within_process():
    a, b = instanceid.identity(), instanceid.identity()
    assert a == b, "the instance id must be stable for the life of the process"
    assert len(instanceid.INSTANCE_ID) == 12
    blob = repr(a)
    # It must not leak anything about the box it runs on.
    for leaky in (os.getcwd(), os.environ.get("HOME", "/root")):
        if leaky:
            assert leaky not in blob


def test_store_revision_is_monotonic_across_events(tmp_path):
    s = Store(path=str(tmp_path / "guild.json"))
    seen = [s.revision]
    for i in range(5):
        s.record_event(None, "query", ua=f"probe/{i}")
        seen.append(s.revision)
    assert seen == sorted(seen), "revision must never go backwards"
    assert seen[-1] > seen[0]


def test_state_diagnostics_reports_agreement_under_sqlite(tmp_path):
    s = _sqlite_store(tmp_path)
    s.record_event(None, "query", ua="probe/1")
    d = s.state_diagnostics()
    assert d["instance"] == instanceid.INSTANCE_ID
    assert d["store_mode"] == "sqlite"
    assert d["divergence"] == [], d
    assert d["consistent"] is True
    assert d["in_memory"]["events"] == d["durable"]["events"]
    # no paths, no secrets
    assert str(tmp_path) not in repr(d)


def test_state_diagnostics_DETECTS_a_stale_in_memory_view(tmp_path):
    """THE detector. Two Stores over one SQLite file: one writes, the other's
    in-memory view is now behind the database it shares. The ops pass must be
    able to SEE that, rather than being told to discard the first few reads."""
    writer = _sqlite_store(tmp_path)
    reader = _sqlite_store(tmp_path)          # hydrated at its own boot
    before = reader.state_diagnostics()
    assert before["divergence"] == []

    for i in range(3):
        writer.record_event(None, "query", ua=f"late/{i}")

    after = reader.state_diagnostics()
    assert "durable_events_ahead_of_in_memory" in after["divergence"]
    assert after["consistent"] is False
    assert after["durable"]["events"] > after["in_memory"]["events"]


# --------------------------------------------------------------------------
# 2. Canonical writes fail CLOSED
# --------------------------------------------------------------------------
def test_publish_refuses_when_durable_head_is_behind_observed_head(tmp_path):
    """The exact production signature: a publish computed from a view two
    entries behind the committed feed. It must refuse, not publish."""
    s = _sqlite_store(tmp_path)
    s.record_event(None, "query", ua="seed/1")
    first = s.publish_checkpoint()
    assert first["index"] == 0

    # Simulate the observed condition: this process has seen head 5 published,
    # but the authoritative feed only has head 0.
    s.checkpoints = list(s.checkpoints) + [
        {"index": 5, "published_at": "2026-07-31T00:00:00+00:00",
         "ledger_length": 999, "checkpoint": {"head_hash": "deadbeef"}}]

    with pytest.raises(StaleDurableStateError) as exc:
        s.publish_checkpoint()
    assert "BEHIND" in str(exc.value)
    assert exc.value.code == "stale_durable_state"
    # and nothing was written
    assert s.backend.durable_counts()["checkpoints"] == 1


def test_publish_refuses_a_short_durable_ledger(tmp_path):
    s = _sqlite_store(tmp_path)
    s.record_event(None, "query", ua="seed/1")
    s.publish_checkpoint()
    # in-memory ledger claims more records than are committed
    s.ledger_records = list(s.ledger_records) + [{"seq": 10 ** 6, "fake": True}]
    with pytest.raises(StaleDurableStateError) as exc:
        s.publish_checkpoint()
    assert "SHORTER" in str(exc.value)


def test_publish_refuses_to_overwrite_an_existing_index_fork(tmp_path):
    """Two publishers racing on the same next index used to be an INSERT OR
    REPLACE — silently replacing a commitment a third party may already hold."""
    s = _sqlite_store(tmp_path)
    s.record_event(None, "query", ua="seed/1")
    entry = s.publish_checkpoint()
    with pytest.raises(CheckpointForkError):
        s.backend.insert_checkpoint_strict(dict(entry))
    assert s.backend.durable_counts()["checkpoints"] == 1


def test_publish_is_idempotent_when_no_evidence_landed(tmp_path):
    s = _sqlite_store(tmp_path)
    s.record_event(None, "query", ua="seed/1")
    a = s.publish_checkpoint()
    b = s.publish_checkpoint()
    c = s.publish_checkpoint()
    assert a["index"] == b["index"] == c["index"]
    assert s.backend.durable_counts()["checkpoints"] == 1


def test_publish_read_after_write_verifies_the_stored_bytes(tmp_path):
    s = _sqlite_store(tmp_path)
    s.record_event(None, "query", ua="seed/1")
    entry = s.publish_checkpoint()
    stored = s.backend.checkpoint_at(entry["index"])
    assert stored == entry, "the feed must read back byte-identical"


def test_publish_reports_unverified_write_instead_of_success(tmp_path, monkeypatch):
    """If the row cannot be read back, the publish must NOT be reported as
    published — the failure mode that turns a missing commitment into a
    confident lie."""
    s = _sqlite_store(tmp_path)
    s.record_event(None, "query", ua="seed/1")
    monkeypatch.setattr(s.backend, "checkpoint_at", lambda idx: None)
    with pytest.raises(CheckpointWriteVerificationError):
        s.publish_checkpoint()


def test_next_index_comes_from_max_index_not_list_length(tmp_path):
    """A feed with a gap must not re-issue an index that already exists.

    GAP POLICY (2026-08-01). A gapped feed now REFUSES by default: a hole means
    a published commitment is missing from this view, and extending it would
    produce a chain a verifier cannot walk. The original intent of this test —
    never re-issue an existing index — is preserved and asserted on the far
    side of an explicit, gap-shaped authorization.
    """
    import os
    import pytest
    from app.store import CanonicalWriteRefused

    s = _sqlite_store(tmp_path)
    s.record_event(None, "query", ua="seed/1")
    s.publish_checkpoint()                      # index 0
    # a hand-repaired feed with a gap (indices 0 and 4 present, length 2)
    gap = {"index": 4, "published_at": "2026-07-31T00:00:00+00:00",
           "ledger_length": 1, "checkpoint": {"head_hash": "x"}}
    s.backend.insert_checkpoint_strict(gap)
    s.checkpoints = s.backend.all_checkpoints()
    assert len(s.checkpoints) == 2
    s.ledger_records = s.backend.all_ledger()
    s.record_event(None, "query", ua="seed/2")

    # (a) DEFAULT: refuse, naming the exact missing indices.
    os.environ.pop("GUILD_CANONICAL_ACCEPT_GAPS", None)
    with pytest.raises(CanonicalWriteRefused) as exc:
        s.publish_checkpoint()
    assert "missing indices [1, 2, 3]" in str(exc.value)

    # (b) a WRONGLY-SHAPED authorization does not apply
    own = (s.identity or {}).get("did")
    os.environ["GUILD_CANONICAL_ACCEPT_GAPS"] = f"{own}:1,2"
    with pytest.raises(CanonicalWriteRefused):
        s.publish_checkpoint()

    # (c) the EXACT gap, authorised: extend at max+1, never re-issue.
    os.environ["GUILD_CANONICAL_ACCEPT_GAPS"] = f"{own}:1,2,3"
    try:
        entry = s.publish_checkpoint()
        assert entry["index"] == 5, (
            "next index must come from max(index)+1, not len(feed)")
        idxs = [e["index"] for e in s.backend.all_checkpoints()]
        assert sorted(idxs) == [0, 4, 5]
        assert len(idxs) == len(set(idxs))
    finally:
        os.environ.pop("GUILD_CANONICAL_ACCEPT_GAPS", None)


def test_concurrent_publishers_never_fork_the_feed(tmp_path):
    """Shared-store concurrency: many threads publishing at once must produce a
    strictly increasing, gap-free, non-overwritten feed — or refuse."""
    s = _sqlite_store(tmp_path)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            s.record_event(None, "query", ua=f"racer/{i}")
            s.publish_checkpoint()
        except CanonicalWriteRefused as exc:      # refusing is an ACCEPTED outcome
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    feed = s.backend.all_checkpoints()
    idxs = [e["index"] for e in feed]
    assert idxs == sorted(set(idxs)), f"feed forked or duplicated: {idxs}"
    assert idxs == list(range(len(idxs))), f"feed has a gap: {idxs}"
    # every entry commits to its predecessor
    for prev, cur in zip(feed, feed[1:]):
        assert cur.get("prev_entry_sha256"), "continuity commitment missing"


# --------------------------------------------------------------------------
# 3. Transaction-depth hygiene (thread-local connection poisoning)
# --------------------------------------------------------------------------
def test_nested_transaction_rollback_does_not_poison_the_thread(tmp_path):
    """A nested txn that raises used to leave this THREAD's depth at -1, after
    which every later 'transaction' silently ran in autocommit — losing
    atomicity for the life of the process, on that thread only."""
    s = _sqlite_store(tmp_path)
    b = s.backend
    try:
        with b.transaction():
            try:
                with b.transaction():
                    raise ValueError("inner blows up")
            except ValueError:
                pass
    except Exception:
        pass
    assert getattr(b._local, "depth", 0) >= 0, "depth went negative"
    # and a real transaction still opens afterwards
    with b.transaction():
        assert b.in_transaction() is True
    assert b.in_transaction() is False
    # writes still land
    s.record_event(None, "query", ua="after/1")
    assert s.backend.durable_counts()["events"] >= 1


def test_durable_slightly_ahead_mid_snapshot_is_not_divergence(tmp_path, monkeypatch):
    """The diagnostic samples the in-memory count, then queries SQLite. Live
    traffic lands in between, so 'durable is a few ahead' is the NORMAL
    interleaving. Reporting it as divergence would fire on every run."""
    s = _sqlite_store(tmp_path)
    s.record_event(None, "query", ua="a")
    real = s.backend.durable_counts

    def _racy():
        out = real()
        # a concurrent request lands between the two observations
        s.record_event(None, "query", ua="concurrent")
        return out

    monkeypatch.setattr(s.backend, "durable_counts", _racy)
    d = s.state_diagnostics()
    assert d["divergence"] == [], d
    assert "events_after_durable_read" in d["in_memory"]


def test_in_memory_ahead_of_durable_is_still_divergence(tmp_path, monkeypatch):
    """The dangerous direction must NOT be softened by the race tolerance:
    events held only in memory are lost on restart."""
    s = _sqlite_store(tmp_path)
    for i in range(4):
        s.record_event(None, "query", ua=f"a{i}")
    monkeypatch.setattr(s.backend, "durable_counts",
                        lambda: {"events": 0, "agents": 0, "ledger_records": 0,
                                 "checkpoints": 0, "checkpoint_head_index": None,
                                 "checkpoint_head_hash": None})
    d = s.state_diagnostics()
    assert "in_memory_events_ahead_of_durable" in d["divergence"]
