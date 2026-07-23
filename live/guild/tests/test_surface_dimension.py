"""The `surface` event dimension (passport programme 2026-07-23).

Every instrumentation event is stamped at write time with the transport
surface it arrived on — 'mcp' (key='mcp' or a self-identified 'mcp:<client>'
UA), 'a2a' (actor key or UA namespaced 'a2a:'), else 'http' — derived from
the conventions the transports already use, so NO call site changes. Both
persistence backends carry the field through a restart; events predating it
are read honestly as 'unknown_surface' by the passport funnel, never
backfilled.
"""
import os

os.environ.setdefault("GUILD_DATA", "")

import pytest  # noqa: E402

from app.store import Store  # noqa: E402


def _event(store, marker):
    return next(e for e in store.events if e.get("marker") == marker)


def test_surface_derivation_rules():
    s = Store(path="")
    s.record_event("mcp", "delegation", ua="mcp/remote", marker="m1")
    s.record_event(None, "query", ua="mcp:claude-desktop/1.2", marker="m2")
    s.record_event("a2a:net:abc123", "query", ua="a2a:python-httpx/0.28",
                   marker="m3")
    s.record_event("a2a:anon:def456", "offer_served", ua="", marker="m4")
    s.record_event(None, "offer_served", ua="python-httpx/0.28", marker="m5")
    s.record_event(None, "register", ua="", marker="m6")
    assert _event(s, "m1")["surface"] == "mcp"      # key == "mcp"
    assert _event(s, "m2")["surface"] == "mcp"      # ua mcp:<client>
    assert _event(s, "m3")["surface"] == "a2a"      # a2a: ua + key
    assert _event(s, "m4")["surface"] == "a2a"      # a2a: key alone
    assert _event(s, "m5")["surface"] == "http"     # plain framework UA
    assert _event(s, "m6")["surface"] == "http"     # default


def test_surface_persists_across_restart_json_store(tmp_path):
    data = str(tmp_path / "guild.json")
    s = Store(path=data)
    s.record_event("mcp", "delegation", ua="mcp/remote", marker="jsonp")
    s._save()
    reloaded = Store(path=data)
    assert _event(reloaded, "jsonp")["surface"] == "mcp"


def test_surface_persists_across_restart_sqlite_store(tmp_path, monkeypatch):
    db = str(tmp_path / "guild.sqlite3")
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    monkeypatch.setenv("GUILD_STORE_PATH", db)
    s = Store(path="")
    assert s.store_mode == "sqlite" and s.backend is not None
    s.record_event(None, "query", ua="a2a:external-bot/2.0", marker="sqlp")
    reloaded = Store(path="")
    ev = _event(reloaded, "sqlp")
    assert ev["surface"] == "a2a"
    assert ev["ua"] == "a2a:external-bot/2.0"


def test_pre_surface_events_read_as_unknown_surface_in_funnel():
    s = Store(path="")
    # a historical event written before the field existed: appended raw, the
    # way _load replays old journals — no surface key at all.
    s.events.append({"key": "anon", "type": "passport_verified", "ua": "",
                     "fp": False, "at": "2026-07-01T00:00:00+00:00"})
    stage = next(r for r in s.passport_funnel()["stages"]
                 if r["stage"] == "passport_verified")
    assert stage["by_surface"].get("unknown_surface") == 1
