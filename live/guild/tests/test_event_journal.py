"""Instrumentation events are durable across a process restart.

Locks the fix for the 2026-07-06 event-loss incident: record_event used to be
memory-only (persisted only when an unrelated write called _save), so a deploy
restart erased every event since the last write — including a genuine external
agent's entire passport funnel and two days of retention signals. record_event
now appends to an O(1) JSONL sidecar journal, replayed on load and compacted
into the main file on _save.
"""
import json
import os
import tempfile

from app.store import Store


def _fresh_path():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)  # Store creates it on first save
    return path


def _cleanup(path):
    for p in (path, path + ".events.jsonl", path + ".tmp"):
        if os.path.exists(p):
            os.remove(p)


def test_read_path_events_survive_restart_without_any_save():
    """The incident case: only read-path events (no _save-triggering write)."""
    path = _fresh_path()
    try:
        s1 = Store(path=path)
        s1.record_event(None, "check", ua="mcp:some-external/1.0",
                        endpoint="best_agent", paid=False)
        s1.record_event(None, "passport_verified", ua="mcp:some-external/1.0",
                        endpoint="verify")
        # deliberately NO _save() — simulate a restart right here
        s2 = Store(path=path)
        types = [e["type"] for e in s2.events]
        assert types == ["check", "passport_verified"]
        assert s2.events[0]["ua"] == "mcp:some-external/1.0"
    finally:
        _cleanup(path)


def test_save_compacts_journal_and_reload_does_not_duplicate():
    path = _fresh_path()
    try:
        s1 = Store(path=path)
        s1.record_event(None, "check", ua="x", endpoint="best_agent")
        s1._save()
        # compaction: event now lives in the main file, journal truncated
        assert os.path.getsize(path + ".events.jsonl") == 0
        s1.record_event(None, "reputation", ua="y", endpoint="reputation")
        s2 = Store(path=path)
        assert [e["type"] for e in s2.events] == ["check", "reputation"]
    finally:
        _cleanup(path)


def test_crash_window_overlap_deduplicates():
    """If _save wrote the main file but the truncate never landed, replay must
    not double-count the journaled copies of already-compacted events."""
    path = _fresh_path()
    try:
        s1 = Store(path=path)
        s1.record_event(None, "check", ua="x", endpoint="best_agent")
        journal = open(path + ".events.jsonl").read()
        s1._save()
        with open(path + ".events.jsonl", "w") as f:
            f.write(journal)  # resurrect the pre-compaction journal
        s2 = Store(path=path)
        assert len([e for e in s2.events if e["type"] == "check"]) == 1
    finally:
        _cleanup(path)


def test_torn_journal_line_is_skipped():
    path = _fresh_path()
    try:
        s1 = Store(path=path)
        s1.record_event(None, "check", ua="x", endpoint="best_agent")
        with open(path + ".events.jsonl", "a") as f:
            f.write('{"key": "anon", "type": "trunc')  # torn write at crash
        s2 = Store(path=path)
        assert [e["type"] for e in s2.events] == ["check"]
    finally:
        _cleanup(path)


def test_journal_lines_are_valid_json_events():
    path = _fresh_path()
    try:
        s1 = Store(path=path)
        s1.record_event(None, "capability_demand", ua="a2a:probe/1.0",
                        capability="fact-check")
        lines = open(path + ".events.jsonl").read().splitlines()
        assert len(lines) == 1
        e = json.loads(lines[0])
        assert e["type"] == "capability_demand" and e["capability"] == "fact-check"
        assert e["at"]  # timestamp present for replay ordering
    finally:
        _cleanup(path)
