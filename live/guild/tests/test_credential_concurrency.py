"""Credential lifecycle invariants under concurrency (refinement 2026-07-10).

Two simultaneous rotations against one agent record must leave EXACTLY ONE
valid credential — never two. The store serialises credential mutation under
its lock, so the last write wins and every earlier-returned key is dead.
"""
import os, tempfile, threading

os.environ.setdefault("GUILD_DATA", os.path.join(tempfile.mkdtemp(), "g.json"))

import app.credentials as creds
from app.store import Store


def _fresh():
    import uuid
    return Store(path=os.path.join(tempfile.mkdtemp(), f"{uuid.uuid4().hex}.json"))


def test_concurrent_rotations_leave_exactly_one_valid_credential():
    s = _fresh()
    a = s.register_agent("Racer", ["x"], {})
    issued = []
    barrier = threading.Barrier(6)

    def rotate():
        barrier.wait()                       # maximise overlap
        try:
            issued.append(s.rotate_api_key(a["id"])["api_key"])
        except Exception as e:               # collision-guard exhaustion etc.
            issued.append(("err", repr(e)))

    threads = [threading.Thread(target=rotate) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    keys = [k for k in issued if isinstance(k, str)]
    rec = s.get_agent(a["id"])
    valid = [k for k in keys if creds.verify_agent_key(rec, k)]
    assert len(valid) == 1, f"{len(valid)} valid credentials survived: {valid}"
    # and it is the one currently stored
    assert creds.verify_agent_key(rec, valid[0])


def test_concurrent_rotate_and_revoke_resolve_deterministically():
    s = _fresh()
    a = s.register_agent("RaceRR", ["x"], {})
    outcomes = []
    barrier = threading.Barrier(2)

    def rot():
        barrier.wait(); outcomes.append(("rotate", s.rotate_api_key(a["id"])["api_key"]))

    def rev():
        barrier.wait(); s.revoke_api_key(a["id"]); outcomes.append(("revoke", None))

    t1, t2 = threading.Thread(target=rot), threading.Thread(target=rev)
    t1.start(); t2.start(); t1.join(); t2.join()
    rec = s.get_agent(a["id"])
    # exactly one of two deterministic end-states: revoked-last (no active key)
    # or rotated-last (exactly the rotated key authenticates, nothing else)
    if not creds.agent_has_active_key(rec):
        rotated = [k for _, k in outcomes if _ == "rotate"][0]
        assert not creds.verify_agent_key(rec, rotated)   # revoke won
    else:
        rotated = [k for _, k in outcomes if _ == "rotate"][0]
        assert creds.verify_agent_key(rec, rotated)        # rotate won


def test_revoked_credential_cannot_rotate_or_restore_itself():
    s = _fresh()
    a = s.register_agent("Revoked", ["x"], {})
    old = a["api_key"]
    s.revoke_api_key(a["id"])
    rec = s.get_agent(a["id"])
    assert not creds.verify_agent_key(rec, old)            # dead
    assert not creds.agent_has_active_key(rec)
    # a fresh operator rotation re-issues; the OLD key still never works
    new = s.rotate_api_key(a["id"])["api_key"]
    rec = s.get_agent(a["id"])
    assert creds.verify_agent_key(rec, new)
    assert not creds.verify_agent_key(rec, old)


def test_rotation_generates_a_different_identifier(monkeypatch):
    monkeypatch.setenv("GUILD_HASH_KEYS", "1")
    monkeypatch.setenv("GUILD_ALLOW_WEAK_KDF", "1")
    monkeypatch.setenv("GUILD_KDF_ITERS", "1000")
    monkeypatch.setenv("GUILD_ALLOW_WEAK_KDF", "1")
    s = _fresh()
    a = s.register_agent("KidRotate", ["x"], {})
    kid1 = s.get_agent(a["id"])["key_id"]
    s.rotate_api_key(a["id"])
    kid2 = s.get_agent(a["id"])["key_id"]
    assert kid1 and kid2 and kid1 != kid2
    assert len(kid2) == creds.KEY_ID_LEN == 32            # 128 bits
