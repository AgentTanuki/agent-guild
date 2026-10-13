"""Daily ledger sealing on the swarm cadence (2026-10-13): at most once per
SEAL_INTERVAL_S, idempotent, no admin token involved."""
import os
import time

os.environ["GUILD_DATA"] = ""

from app.store import Store  # noqa: E402
from app.swarm import runner  # noqa: E402


def _store(tmp_path):
    return Store(path=str(tmp_path / "g.json"))


def test_seals_when_no_checkpoint_exists(tmp_path):
    s = _store(tmp_path)
    out = runner._maybe_seal_checkpoint(s)
    assert out["action"] == "sealed"
    assert s.latest_checkpoint(publish_if_empty=False) is not None


def test_skips_when_head_is_recent(tmp_path):
    s = _store(tmp_path)
    runner._maybe_seal_checkpoint(s)
    head = s.latest_checkpoint(publish_if_empty=False)
    out = runner._maybe_seal_checkpoint(s)
    assert out["action"] == "skipped" and out["reason"] == "recent"
    assert out["index"] == head["index"]


def test_seals_again_after_the_interval(tmp_path):
    s = _store(tmp_path)
    first = runner._maybe_seal_checkpoint(s)
    later = time.time() + runner.SEAL_INTERVAL_S + 60
    out = runner._maybe_seal_checkpoint(s, now=later)
    # publish_checkpoint is idempotent when nothing landed: the feed head is
    # returned, not duplicated — the helper reports the (possibly same) index.
    assert out["action"] == "sealed"
    assert out["index"] >= first["index"]
