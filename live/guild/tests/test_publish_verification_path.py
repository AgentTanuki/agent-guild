"""The VERIFICATION path around publish — the thing that actually broke.

THE PHANTOM INCIDENT (2026-07-31 -> 2026-08-02)
-----------------------------------------------
Three consecutive daily operations passes reported that
``POST /ledger/checkpoint/publish`` returned a superseded checkpoint (index 14 /
834 records) while the feed head was 17 / 840. It was escalated as a write-path
integrity incident.

Production never did it. The ops pass wrote curl's output to a SHARED
``/tmp/pub.json`` it did not own; the write failed; only ``%{http_code}`` was
checked, so the failure was invisible; and the next command parsed a four-day-old
file and reported it as the live response. Re-running the publish against a
writable path returns 17 / 840 every time.

The defect was in the verification path, so that is what these tests pin. They
are deliberately built around ONE question: *can a stale or replayed body still
be reported as a successful live publish?* Every case below answers "no" for a
structural reason, not because a human remembered to look.

Covered here:
  * the success response identifies its own origin (the 200 used to be the only
    unattributable response the service produced);
  * `observed_at` moves per call, so a replay is detectable;
  * the ops script refuses a body with no `view`, a frozen `observed_at`, a
    below-floor index, or a claim the feed and /health do not corroborate;
  * the returned ARTEFACT — not merely the view it was computed from — is
    gated against the canonical floor on BOTH paths out of publish_checkpoint.

Assertions are on observable HTTP/CLI behaviour, never on helper return values:
the previous hardening round passed its unit tests while the operators' picture
of production stayed wrong for three days.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib

import pytest

os.environ["GUILD_DATA"] = ""          # in-memory only

from fastapi.testclient import TestClient          # noqa: E402

from app.main import app                            # noqa: E402
from app.store import CanonicalFloorRegressionError, Store   # noqa: E402


_SCRIPT = (pathlib.Path(__file__).resolve().parents[3]
           / "live" / "scripts" / "ops_publish_checkpoint.py")


def _load_ops_module():
    spec = importlib.util.spec_from_file_location("ops_publish", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ops = _load_ops_module()


@pytest.fixture()
def client_admin():
    """TestClient + admin token.

    ``GUILD_ADMIN_TOKEN`` is unset under the suite, so `main.ADMIN_TOKEN` is
    falsy and the header is not enforced. The token is still SENT, so these
    tests exercise the same call shape the scheduled ops pass makes."""
    return TestClient(app), "test-admin"


# --------------------------------------------------------------------------
# 1. THE SUCCESS RESPONSE IDENTIFIES ITSELF
# --------------------------------------------------------------------------
def test_publish_200_carries_view_identity(client_admin):
    client, token = client_admin
    r = client.post("/ledger/checkpoint/publish",
                    headers={"X-Admin-Token": token})
    assert r.status_code == 200, r.text
    view = r.json().get("view")
    assert isinstance(view, dict), "the 200 must carry a view block"
    for key in ("instance", "boot_at", "pid", "observed_at", "store_mode",
                "store_rev", "returned_checkpoint_index",
                "returned_ledger_length", "floor_checkpoint_index",
                "floor_ledger_length", "floor_sources"):
        assert key in view, f"view is missing {key}"
    # the view must describe the checkpoint actually returned
    assert (view["returned_checkpoint_index"]
            == r.json()["checkpoint"]["index"])


def test_view_carries_no_secrets_or_paths(client_admin):
    """The block is served publicly on a route an operator may paste around."""
    client, token = client_admin
    view = client.post("/ledger/checkpoint/publish",
                       headers={"X-Admin-Token": token}).json()["view"]
    blob = json.dumps(view).lower()
    for leak in ("/data", "/app", "sqlite3", "token", "secret", "password",
                 "guild_admin", os.environ.get("GUILD_STORE_PATH", "\0zzz")):
        assert leak.lower() not in blob, f"view leaked {leak!r}"


def test_observed_at_moves_between_calls(client_admin):
    """A body byte-identical across two calls is a recording, not a commitment.

    This is the property that would have exposed the phantom incident on day
    one: the ops pass 'saw' three identical bodies and had no way to tell that
    was impossible."""
    client, token = client_admin
    h = {"X-Admin-Token": token}
    a = client.post("/ledger/checkpoint/publish", headers=h).json()
    b = client.post("/ledger/checkpoint/publish", headers=h).json()
    assert a["checkpoint"]["index"] == b["checkpoint"]["index"], \
        "idempotent publish must not advance the head"
    assert a["view"]["observed_at"] != b["view"]["observed_at"], \
        "two live publishes must be distinguishable from one replayed body"
    assert a["view"]["instance"] == b["view"]["instance"], \
        "one process must report one stable instance id"


# --------------------------------------------------------------------------
# 2. THE RETURNED ARTEFACT IS GATED, NOT ONLY THE VIEW IT CAME FROM
# --------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["sqlite", "json"])
def test_idempotent_return_refuses_below_operator_floor(tmp_path, monkeypatch,
                                                        mode):
    """A feed head below the proven floor is never handed back with success.

    REGRESSION COVERAGE FOR THE EXISTING VIEW CHECK — this case already failed
    closed before 2026-08-02, and it is pinned here because the idempotent
    branch returns an entry it *selected* rather than one it built, which is the
    path most likely to hand a caller a superseded commitment."""
    monkeypatch.setenv("GUILD_STORE", mode)
    monkeypatch.setenv("GUILD_ALLOW_WEAK_KDF", "1")
    if mode == "sqlite":
        monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "g.sqlite3"))
    st = Store(str(tmp_path / "g.json"))
    st.register_agent(name="a", capabilities=["fact-check"], metadata={})
    first = st.publish_checkpoint()

    # No new evidence -> the next call takes the idempotent path. Now assert an
    # operator floor ABOVE the head: the entry about to be returned is stale.
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_INDEX", str(int(first["index"]) + 3))
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_LENGTH",
                       str(int(first["ledger_length"]) + 10))
    with pytest.raises(CanonicalFloorRegressionError):
        st.publish_checkpoint()


@pytest.mark.parametrize("mode", ["sqlite", "json"])
def test_returned_artefact_gate_is_independent_of_the_view_check(
        tmp_path, monkeypatch, mode):
    """The LAST gate holds on its own — defence in depth, stated honestly.

    `_assert_canonical_floor` validates the VIEW a publish is computed from and
    already catches every below-floor case reachable today; the artefact gate
    added on 2026-08-02 is REDUNDANT with it on the current code paths. It earns
    its place by being independent: any future return path that skips, weakens
    or short-circuits the view check still cannot emit a below-floor commitment
    with a success status.

    So the view check is neutralised here deliberately. Passing this test with
    the artefact gate removed is impossible; passing it is the only evidence
    that the redundancy is real rather than decorative."""
    monkeypatch.setenv("GUILD_STORE", mode)
    monkeypatch.setenv("GUILD_ALLOW_WEAK_KDF", "1")
    if mode == "sqlite":
        monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "g.sqlite3"))
    st = Store(str(tmp_path / "g.json"))
    st.register_agent(name="a", capabilities=["fact-check"], metadata={})
    first = st.publish_checkpoint()

    monkeypatch.setattr(st, "_assert_canonical_floor",
                        lambda *a, **k: None)      # view check disabled
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_INDEX", str(int(first["index"]) + 5))
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_LENGTH",
                       str(int(first["ledger_length"]) + 20))

    # idempotent path
    with pytest.raises(CanonicalFloorRegressionError, match="BELOW the proven"):
        st.publish_checkpoint()
    # append path — new evidence, so a fresh entry is built and must be gated too
    st.register_agent(name="b", capabilities=["fact-check"], metadata={})
    with pytest.raises(CanonicalFloorRegressionError, match="BELOW the proven"):
        st.publish_checkpoint()


@pytest.mark.parametrize("mode", ["sqlite", "json"])
def test_a_refused_publish_does_not_raise_the_high_water_mark(tmp_path,
                                                              monkeypatch,
                                                              mode):
    """A below-floor entry must not raise the floor that should reject it.

    Ordering invariant: the artefact gate runs BEFORE `_record_canonical_hwm`.
    If it ran after, a single refused publish would move the high-water mark to
    the stale position and every subsequent publish would be 'above the floor'
    — the guard would disarm itself on first use."""
    monkeypatch.setenv("GUILD_STORE", mode)
    monkeypatch.setenv("GUILD_ALLOW_WEAK_KDF", "1")
    if mode == "sqlite":
        monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "g.sqlite3"))
    st = Store(str(tmp_path / "g.json"))
    st.register_agent(name="a", capabilities=["fact-check"], metadata={})
    st.publish_checkpoint()
    before = dict(st.canonical_hwm or {})

    monkeypatch.setattr(st, "_assert_canonical_floor", lambda *a, **k: None)
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_INDEX", "500")
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_LENGTH", "5000")
    with pytest.raises(CanonicalFloorRegressionError):
        st.publish_checkpoint()

    assert dict(st.canonical_hwm or {}) == before, (
        "a refused publish moved the high-water mark")


def test_refusal_is_409_not_a_200(client_admin, monkeypatch):
    """Fail CLOSED with an explicit non-success status — never a quiet 200."""
    client, token = client_admin
    client.post("/ledger/checkpoint/publish", headers={"X-Admin-Token": token})
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_INDEX", "9999")
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_LENGTH", "999999")
    r = client.post("/ledger/checkpoint/publish",
                    headers={"X-Admin-Token": token})
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["error"] == "canonical_write_refused"
    assert body["code"] == "canonical_floor_regression"
    assert "view" in body


# --------------------------------------------------------------------------
# 3. THE OPS SCRIPT REFUSES EVERY SHAPE OF UNPROVEN RESPONSE
# --------------------------------------------------------------------------
def _fake_transport(responses):
    """Drive the ops script's single I/O seam with canned bodies."""
    calls = []

    def _req(url, *, token=None, method="GET"):
        calls.append((method, url))
        key = "publish" if url.endswith("/publish") else (
            "checkpoints" if "/ledger/checkpoints" in url else "health")
        val = responses[key]
        return val.pop(0) if isinstance(val, list) else val

    return _req, calls


def _good_publish(idx=17, length=840, observed="t1", instance="aaa"):
    return {"status": "published",
            "checkpoint": {"index": idx, "ledger_length": length,
                           "checkpoint": {"head_hash": "h", "chain_valid": True}},
            "view": {"instance": instance, "observed_at": observed,
                     "floor_checkpoint_index": idx,
                     "floor_ledger_length": length}}


def _surfaces(idx=17, length=840):
    return {
        "checkpoints": {"checkpoints": [{"index": idx, "ledger_length": length}]},
        "health": {"canonical_state": {
            "ok": True, "served_checkpoint_index": idx,
            "floor_checkpoint_index": idx, "served_ledger_length": length,
            "floor_ledger_length": length}},
    }


def test_ops_script_accepts_a_live_consistent_publish(monkeypatch):
    resp = {"publish": [_good_publish(observed="t1"),
                        _good_publish(observed="t2")], **_surfaces()}
    req, _ = _fake_transport(resp)
    monkeypatch.setattr(ops, "_request", req)
    out = ops.publish_verified("https://x", "tok", min_index=17,
                               min_length=840)
    assert out["verified"] and out["checkpoint_index"] == 17


def test_ops_script_refuses_a_body_with_no_view(monkeypatch):
    """A stale file cannot mint a view block — so demand one."""
    stale = _good_publish(idx=14, length=834)
    stale.pop("view")
    resp = {"publish": [stale, stale], **_surfaces()}
    req, _ = _fake_transport(resp)
    monkeypatch.setattr(ops, "_request", req)
    with pytest.raises(ops.Unverified, match="no `view` block"):
        ops.publish_verified("https://x", "tok", min_index=17, min_length=840)


def test_ops_script_refuses_a_frozen_observed_at(monkeypatch):
    """The exact signature of the phantom incident: identical bodies."""
    same = _good_publish(idx=14, length=834, observed="frozen")
    resp = {"publish": [same, same], **_surfaces(idx=14, length=834)}
    req, _ = _fake_transport(resp)
    monkeypatch.setattr(ops, "_request", req)
    with pytest.raises(ops.Unverified, match="SAME observed_at"):
        ops.publish_verified("https://x", "tok", min_index=0, min_length=0)


def test_ops_script_refuses_below_operator_floor(monkeypatch):
    """What the ops pass BELIEVED it saw must exit non-zero, not be reported."""
    resp = {"publish": [_good_publish(14, 834, "t1"),
                        _good_publish(14, 834, "t2")],
            **_surfaces(idx=14, length=834)}
    req, _ = _fake_transport(resp)
    monkeypatch.setattr(ops, "_request", req)
    with pytest.raises(ops.Unverified, match="BELOW the expected floor"):
        ops.publish_verified("https://x", "tok", min_index=17, min_length=840)


def test_ops_script_refuses_when_feed_disagrees(monkeypatch):
    """Cross-surface reconciliation: publish says 14, the feed says 17."""
    resp = {"publish": [_good_publish(14, 834, "t1"),
                        _good_publish(14, 834, "t2")], **_surfaces()}
    req, _ = _fake_transport(resp)
    monkeypatch.setattr(ops, "_request", req)
    with pytest.raises(ops.Unverified, match="feed head"):
        ops.publish_verified("https://x", "tok", min_index=0, min_length=0)


def test_ops_script_refuses_a_malformed_index(monkeypatch):
    """`int()` coercion would have turned "14"/True/2.7 into a position."""
    bad = _good_publish()
    bad["checkpoint"]["index"] = "17"
    resp = {"publish": [bad, bad | {"view": dict(bad["view"],
                                                 observed_at="t2")}],
            **_surfaces()}
    req, _ = _fake_transport(resp)
    monkeypatch.setattr(ops, "_request", req)
    with pytest.raises(ops.Unverified, match="not a valid ordinal"):
        ops.publish_verified("https://x", "tok", min_index=0, min_length=0)


def test_ops_script_never_reads_the_body_from_disk():
    """The root cause, pinned as a source-level invariant.

    The failure was a RESPONSE ROUTED THROUGH A FILE: curl wrote to a shared
    path it did not own, the write failed, and the reader picked up a four-day-
    old body. `_request` is the only seam in this script that produces a body,
    so it must never touch the filesystem.

    Checked by AST rather than substring: `urlopen(` contains `open(`, and a
    grep-shaped assertion that fires on the correct implementation is a test
    that teaches people to delete it."""
    import ast

    tree = ast.parse(_SCRIPT.read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_request")
    banned = {"open", "read_text", "read_bytes", "loadtxt"}
    banned_attr_roots = {"pathlib", "shutil"}
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in banned:
                pytest.fail(f"_request calls {f.id}() — a response body routed "
                            "through a file is the 2026-08-02 defect")
            if isinstance(f, ast.Attribute) and f.attr in banned:
                pytest.fail(f"_request calls .{f.attr}() — a response body "
                            "routed through a file is the 2026-08-02 defect")
        if isinstance(node, ast.Attribute):
            root = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in banned_attr_roots:
                pytest.fail(f"_request reaches into {root.id} — it must only "
                            "speak HTTP")


def test_ops_script_exits_nonzero_when_unverified(monkeypatch, tmp_path,
                                                  capsys):
    """The CLI contract the scheduled pass depends on: refusal is exit 1."""
    tok = tmp_path / "t"
    tok.write_text("secret-token")
    monkeypatch.setattr(ops, "publish_verified",
                        lambda *a, **k: (_ for _ in ()).throw(
                            ops.Unverified("nope")))
    rc = ops.main(["--url", "https://x", "--token-file", str(tok)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "UNVERIFIED" in err
    assert "secret-token" not in err


def test_ops_script_never_prints_the_token(monkeypatch, tmp_path, capsys):
    tok = tmp_path / "t"
    tok.write_text("super-secret-admin-token")
    resp = {"publish": [_good_publish(observed="t1"),
                        _good_publish(observed="t2")], **_surfaces()}
    req, _ = _fake_transport(resp)
    monkeypatch.setattr(ops, "_request", req)
    rc = ops.main(["--url", "https://x", "--token-file", str(tok),
                   "--min-index", "17", "--min-ledger-length", "840"])
    out = capsys.readouterr()
    assert rc == 0
    assert "super-secret-admin-token" not in (out.out + out.err)
