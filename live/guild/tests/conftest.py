"""Test configuration.

The server self-seeds a labelled BOOTSTRAP evaluation cohort on startup (so a
fresh deploy never shows an empty `/evaluation`). For the rest of the suite we
want a clean, deterministic store, so the startup bootstrap is disabled here.
The dedicated bootstrap tests call `seed_bootstrap_evaluation()` directly on a
fresh Store instead, which exercises the same code path without coupling to app
startup.
"""
import os
import pytest

os.environ.setdefault("GUILD_DATA", "")          # in-memory only
os.environ.setdefault("GUILD_BOOTSTRAP_EVAL", "0")  # no auto-seed during tests
# Production SQLite warms the complete signed discovery census at service
# startup. Keep ordinary tests request-local; dedicated cache tests exercise
# the production path explicitly.
os.environ.setdefault("GUILD_DISCOVERY_REACH_CACHE", "0")
# Abuse controls default ON in production; the suite hammers endpoints far
# beyond real-world burst limits, so they are exercised explicitly in
# tests/test_abuse_controls.py and disabled everywhere else.
os.environ.setdefault("GUILD_ABUSE_CONTROLS", "0")


@pytest.fixture()
def fresh_scout_demand(monkeypatch):
    """A scout scenario owns its inputs, not all earlier tests' durable asks.

    Restored history intentionally outlives serving-memory retention. Keep
    the production ten-capability limit while isolating these scenarios from
    unrelated higher-ranked demand accumulated in the shared test store.
    """
    from app.state import store
    original = store.demand_feed_entries
    prior = {row["capability"] for row in original()}
    monkeypatch.setattr(store, "demand_feed_entries", lambda: [
        row for row in original() if row["capability"] not in prior])
