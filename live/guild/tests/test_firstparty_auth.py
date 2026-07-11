"""First-party authentication + metric exclusion (Pilot B Entry Step 1).

The first-party token is a CLASSIFICATION + internal-auth mechanism, not an
admin credential. Proves: valid token -> AG_INTERNAL/AG_TEST; invalid/missing
-> never AG_INTERNAL; constant-time compare; raw token never in events;
AG_TEST/AG_INTERNAL can't touch genuine_external; genuine external still
counted; UA spoofing without the token can't gain first-party; a valid token
grants NO scopes/admin; rotation invalidates the old token; callers fail closed.
"""
import importlib, os, tempfile

import pytest

os.environ.setdefault("GUILD_DATA", os.path.join(tempfile.mkdtemp(), "g.json"))

import app.firstparty as fp
from app.attribution import (caller_class, is_genuine_external,
                             may_count_as_external_growth)

TOKEN = "s3cret-" + "a" * 48


def _reload(env):
    for k in ("GUILD_FIRST_PARTY_TOKEN", "GUILD_FIRST_PARTY_TOKEN_PREV"):
        os.environ.pop(k, None)
    os.environ.update(env)
    importlib.reload(fp)
    return fp


def teardown_module(_):
    _reload({})


def _e(ua="", first_party=False, role=None, at="2026-07-11T10:00:00+00:00", key=None):
    e = {"ua": ua, "fp": first_party, "at": at, "key": key}
    if role:
        e["fp_role"] = role
    return e


# --- token semantics ---------------------------------------------------------
def test_valid_token_is_first_party_strict():
    m = _reload({"GUILD_FIRST_PARTY_TOKEN": TOKEN})
    assert m.strict_mode() is True
    assert m.is_first_party(TOKEN) is True
    assert m.is_first_party(None, TOKEN) is True          # legacy header carries it


def test_invalid_missing_prefix_suffix_tokens_are_not_first_party():
    m = _reload({"GUILD_FIRST_PARTY_TOKEN": TOKEN})
    assert m.is_first_party("wrong") is False
    assert m.is_first_party(None) is False                 # missing
    assert m.is_first_party(TOKEN + "x") is False          # suffix mismatch
    assert m.is_first_party("x" + TOKEN) is False          # prefix mismatch
    assert m.is_first_party(TOKEN[:-1]) is False           # truncated


def test_comparison_is_constant_time_construct():
    import inspect
    src = inspect.getsource(fp.is_first_party)
    assert "compare_digest" in src                         # not '==' on the secret


def test_honor_mode_only_when_token_unset():
    m = _reload({})
    assert m.strict_mode() is False
    assert m.is_first_party("anything") is True            # transitional pre-activation
    assert m.is_first_party(None) is False


# --- classification: valid token -> AG_INTERNAL/AG_TEST ----------------------
def test_first_party_internal_is_ag_internal():
    assert caller_class(_e(first_party=True, role="internal")) == "AG_INTERNAL"


def test_first_party_test_is_ag_test():
    assert caller_class(_e(first_party=True, role="test")) == "AG_TEST"
    # a first-party caller with a test UA is AG_TEST even without the role
    assert caller_class(_e(ua="mcp:guild-canary/1", first_party=True)) == "AG_TEST"


def test_invalid_or_missing_token_never_ag_internal():
    # no fp flag (invalid/missing token in strict mode never sets fp)
    e = _e(ua="python-httpx/0.28", first_party=False)
    assert caller_class(e) != "AG_INTERNAL"
    # a caller that spoofs an internal ROLE header but has no fp is NOT internal
    e2 = _e(ua="python-httpx/0.28", first_party=False, role="internal")
    assert caller_class(e2) != "AG_INTERNAL"


def test_ua_spoof_without_token_cannot_gain_first_party():
    # claiming a first-party-ish UA without fp does not make you AG_INTERNAL
    e = _e(ua="agent-guild-internal-job/1", first_party=False)
    assert caller_class(e) not in ("AG_INTERNAL",)
    assert not is_genuine_external(e) or caller_class(e).startswith("EXTERNAL")


# --- metric exclusion --------------------------------------------------------
def test_ag_internal_and_ag_test_cannot_be_genuine_external():
    for e in (_e(first_party=True, role="internal"),
              _e(first_party=True, role="test"),
              _e(ua="mcp:guild-canary/1", first_party=True)):
        cls = caller_class(e)
        assert not may_count_as_external_growth(cls)
        assert not is_genuine_external(e)


def test_genuine_external_still_counted():
    e = _e(ua="python-httpx/0.28", first_party=False, key="a2a:net:x")
    assert caller_class(e) == "EXTERNAL_UNKNOWN"
    assert is_genuine_external(e)


# --- no authority from the token ---------------------------------------------
def test_first_party_grants_no_scopes_or_admin():
    """The fp flag affects classification only. It must not appear in scope or
    admin decisions — those read the api key / admin token, never fp."""
    import app.credentials as creds
    fp_agent = {"id": "a", "fp": True}                     # first-party marker
    # scopes_of ignores fp entirely (a record with no scopes = legacy least-priv)
    assert "admin" not in creds.scopes_of(fp_agent)
    assert creds.has_scope(fp_agent, "admin") is False
    # firstparty module exposes no scope/admin API
    import inspect
    assert not any(n in dir(fp) for n in ("grant", "admin", "scope", "authorize"))


# --- rotation ----------------------------------------------------------------
def test_rotation_invalidates_old_token():
    new = "n3w-" + "b" * 48
    # dual-token window: both accepted
    m = _reload({"GUILD_FIRST_PARTY_TOKEN": new, "GUILD_FIRST_PARTY_TOKEN_PREV": TOKEN})
    assert m.is_first_party(new) and m.is_first_party(TOKEN)
    # after removing the prev token, the old value fails
    m = _reload({"GUILD_FIRST_PARTY_TOKEN": new})
    assert m.is_first_party(new) is True
    assert m.is_first_party(TOKEN) is False


# --- no raw token in events --------------------------------------------------
def test_raw_token_never_in_events(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("GUILD_FIRST_PARTY_TOKEN", TOKEN)
    import app.main as main_mod, app.swarm.router as router_mod, app.firstparty as fpm
    importlib.reload(fpm); importlib.reload(router_mod); importlib.reload(main_mod)
    with TestClient(main_mod.app) as c:
        c.get("/terms.json", headers={"X-Agent-Guild-First-Party": TOKEN,
                                      "X-Agent-Guild-Role": "internal"})
        c.get("/.well-known/ag-identities/index.json",
              headers={"X-Agent-Guild-First-Party": TOKEN})
    import json as _j
    dump = _j.dumps(main_mod.store.events)
    assert TOKEN not in dump
    # the tagged event carries fp/fp_role, never the token
    fp_events = [e for e in main_mod.store.events if e.get("fp")]
    assert fp_events, "expected a first-party-tagged event"
    assert all(TOKEN not in _j.dumps(e) for e in fp_events)


def test_terms_read_with_valid_token_classifies_first_party(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("GUILD_FIRST_PARTY_TOKEN", TOKEN)
    import app.main as main_mod, app.swarm.router as router_mod, app.firstparty as fpm
    importlib.reload(fpm); importlib.reload(router_mod); importlib.reload(main_mod)
    with TestClient(main_mod.app) as c:
        # valid token, role internal -> AG_INTERNAL
        c.get("/terms.json", headers={"X-Agent-Guild-First-Party": TOKEN,
                                      "X-Agent-Guild-Role": "internal"})
        ev = [e for e in main_mod.store.events if e["type"] == "swarm_terms_fetch"][-1]
        assert ev.get("fp") is True and ev.get("fp_role") == "internal"
        assert caller_class(ev) == "AG_INTERNAL"
        # WRONG token -> not first-party -> not AG_INTERNAL
        c.get("/terms.json", headers={"X-Agent-Guild-First-Party": "nope"})
        ev2 = [e for e in main_mod.store.events if e["type"] == "swarm_terms_fetch"][-1]
        assert not ev2.get("fp")
        assert caller_class(ev2) != "AG_INTERNAL"
