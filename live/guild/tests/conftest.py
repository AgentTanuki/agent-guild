"""Test configuration.

The server self-seeds a labelled BOOTSTRAP evaluation cohort on startup (so a
fresh deploy never shows an empty `/evaluation`). For the rest of the suite we
want a clean, deterministic store, so the startup bootstrap is disabled here.
The dedicated bootstrap tests call `seed_bootstrap_evaluation()` directly on a
fresh Store instead, which exercises the same code path without coupling to app
startup.
"""
import os

os.environ.setdefault("GUILD_DATA", "")          # in-memory only
os.environ.setdefault("GUILD_BOOTSTRAP_EVAL", "0")  # no auto-seed during tests
# Abuse controls default ON in production; the suite hammers endpoints far
# beyond real-world burst limits, so they are exercised explicitly in
# tests/test_abuse_controls.py and disabled everywhere else.
os.environ.setdefault("GUILD_ABUSE_CONTROLS", "0")
