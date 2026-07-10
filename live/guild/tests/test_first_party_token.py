"""GUILD_FIRST_PARTY_TOKEN strict-mode classification (2026-07-10).

First-party status must be deterministic (token match), never inferred from
UA / IP / naming. Guards both enforcement points (main + swarm router).
"""
import importlib, os


def _reload(token):
    if token is None:
        os.environ.pop("GUILD_FIRST_PARTY_TOKEN", None)
    else:
        os.environ["GUILD_FIRST_PARTY_TOKEN"] = token
    import app.main as m, app.swarm.router as r
    importlib.reload(m); importlib.reload(r)
    return m, r


def test_unset_any_nonempty_header_is_first_party():
    m, r = _reload(None)
    assert m._is_first_party("anything") is True
    assert r._is_first_party("anything") is True
    assert m._is_first_party(None) is False
    assert m._is_first_party("") is False


def test_set_requires_exact_match_both_enforcement_points():
    m, r = _reload("s3cret-token")
    assert m._is_first_party("s3cret-token") is True
    assert r._is_first_party("s3cret-token") is True
    # a third party self-tagging with a guessy header no longer passes
    assert m._is_first_party("first-party") is False
    assert m._is_first_party("test") is False
    assert r._is_first_party("wrong") is False
    assert m._is_first_party(None) is False


def teardown_module(_):
    _reload(None)
