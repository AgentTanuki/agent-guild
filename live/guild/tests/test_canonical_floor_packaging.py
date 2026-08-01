"""The canonical floor pin must actually be IN the deployed image.

Production on 2.0.3 reported `floor_checkpoint_index: -1, floor_sources: []`.
The image floor — the half of the guard whose entire purpose is to survive a
wiped or unmounted disk — did not exist, for two compounding reasons:

  1. `render.yaml` built with `dockerContext: ./live/guild`, so `docs/` was not
     in the build context and could not be COPYied even if the Dockerfile had
     asked for it; and
  2. `Store._image_pinned_floor` resolved the pin three levels up from
     `store.py`, which is correct in a repo checkout and lands on `/` in the
     container (`/app/app/store.py`).

So the guard was defeated by packaging, silently, with every unit test green.
These tests close that: one set reads the ACTUAL build configuration and fails
if the pin could not be in the image, the other materialises the exact image
filesystem layout and drives the REAL runtime lookup against it.

None of this weakens fail-closed behaviour: the pin is a FLOOR (it can only
raise the minimum) and remains issuer-scoped, so it constrains only the
deployment whose identity actually published it.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil

import pytest

from app.store import Store

REPO = pathlib.Path(__file__).resolve().parents[3]
DOCKERFILE = REPO / "live" / "guild" / "Dockerfile"
RENDER_YAML = REPO / "render.yaml"
PIN = REPO / "docs" / "checkpoints" / "latest.json"
PIN_REL = "docs/checkpoints/latest.json"


def _dockerfile_lines():
    return [ln.strip() for ln in DOCKERFILE.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _web_service_block():
    """The first `type: web` service in render.yaml, as raw lines."""
    lines = RENDER_YAML.read_text().splitlines()
    out, started = [], False
    for ln in lines:
        if ln.strip().startswith("- type: web"):
            if started:
                break
            started = True
        if started:
            out.append(ln)
    return out


# --------------------------------------------------------------------------
# 1. BUILD CONFIGURATION — fails on the render.yaml/Dockerfile combination
#    that shipped 2.0.3.
# --------------------------------------------------------------------------
def test_the_build_context_can_see_the_pin():
    """`dockerContext: ./live/guild` cannot reach docs/. This is the root of
    the defect and is asserted directly on the deploy blueprint."""
    block = "\n".join(_web_service_block())
    assert "dockerContext:" in block, "no dockerContext declared"
    ctx = [ln.split(":", 1)[1].strip() for ln in block.splitlines()
           if ln.strip().startswith("dockerContext:")][0]
    ctx_path = (REPO / ctx).resolve()
    assert PIN.resolve().is_relative_to(ctx_path), (
        f"dockerContext {ctx!r} resolves to {ctx_path}, which cannot see "
        f"{PIN} — the checkpoint pin can never be COPYied into the image")


def test_the_dockerfile_copies_the_pin_into_the_image():
    copies = [ln for ln in _dockerfile_lines() if ln.upper().startswith("COPY")]
    assert any(PIN_REL in ln for ln in copies), (
        f"no COPY brings {PIN_REL} into the image; copies were: {copies}")


def test_every_dockerfile_copy_source_exists_in_the_build_context():
    """A COPY of a path the context does not contain fails the build. Catching
    it here is cheaper than catching it in a deploy."""
    block = "\n".join(_web_service_block())
    ctx = [ln.split(":", 1)[1].strip() for ln in block.splitlines()
           if ln.strip().startswith("dockerContext:")][0]
    ctx_path = (REPO / ctx).resolve()
    for ln in _dockerfile_lines():
        if not ln.upper().startswith("COPY"):
            continue
        parts = ln.split()[1:]
        for src in parts[:-1]:
            if src.startswith("--"):
                continue
            assert (ctx_path / src).exists(), (
                f"COPY source {src!r} does not exist in build context {ctx}")


def test_the_pin_is_the_single_authoritative_file():
    """One source of truth. The image gets a COPY of it; the repo must not
    grow a second checked-in copy that can drift."""
    dupes = [p for p in REPO.rglob("latest.json")
             if "checkpoints" in p.parts
             and ".git" not in p.parts and "node_modules" not in p.parts]
    assert dupes == [PIN], f"expected exactly one committed pin, found {dupes}"


def test_dockerignore_excludes_nothing_the_build_needs():
    """A .dockerignore is only safe if it provably cannot drop a COPY source.

    The build context was widened from ./live/guild to the repo root so the
    checkpoint pin could be packaged; .dockerignore keeps that context from
    growing unboundedly. This asserts the two changes cannot fight: every path
    the Dockerfile COPYies must survive the ignore rules — most importantly
    docs/checkpoints/latest.json, which is the entire point of the widening."""
    di = REPO / ".dockerignore"
    if not di.exists():
        pytest.skip("no .dockerignore in this tree")
    patterns = [ln.strip() for ln in di.read_text().splitlines()
                if ln.strip() and not ln.strip().startswith("#")]

    def ignored(rel: str) -> bool:
        import fnmatch
        parts = pathlib.PurePosixPath(rel).parts
        for pat in patterns:
            p = pat.lstrip("/")
            # match the whole path, and every parent prefix (docker excludes a
            # directory's entire subtree)
            for i in range(1, len(parts) + 1):
                prefix = "/".join(parts[:i])
                if fnmatch.fnmatch(prefix, p):
                    return True
                if p.startswith("**/") and fnmatch.fnmatch(parts[i - 1], p[3:]):
                    return True
        return False

    copies = [ln for ln in _dockerfile_lines() if ln.upper().startswith("COPY")]
    assert copies
    for ln in copies:
        for src in ln.split()[1:-1]:
            if src.startswith("--"):
                continue
            assert not ignored(src), (
                f".dockerignore excludes COPY source {src!r} — the build would "
                "fail or the image would silently lack it")
    assert not ignored(PIN_REL), "the canonical floor pin is dockerignored"
    # and the things we DO mean to exclude are excluded
    for junk in (".git/config", "live/guild/__pycache__/x.pyc",
                 "node_modules/pkg/index.js"):
        assert ignored(junk), f"expected {junk} to be excluded"


# --------------------------------------------------------------------------
# 2. RUNTIME LOOKUP — in the exact image filesystem layout.
# --------------------------------------------------------------------------
def _materialise_image_layout(tmp_path, *, include_pin: bool):
    """Reproduce what the Dockerfile produces: WORKDIR /app, app at /app/app,
    and (with the fix) the pin at /app/docs/checkpoints/latest.json."""
    app_root = tmp_path / "app"
    (app_root / "app").mkdir(parents=True)
    (app_root / "app" / "store.py").write_text("# placeholder\n")
    if include_pin:
        dst = app_root / "docs" / "checkpoints"
        dst.mkdir(parents=True)
        shutil.copy2(PIN, dst / "latest.json")
    return app_root / "app"          # == dirname(store.py) in the image


def test_runtime_lookup_resolves_the_packaged_pin_in_the_image_layout(tmp_path):
    here = _materialise_image_layout(tmp_path, include_pin=True)
    found = [c for c in Store._image_pin_candidates(str(here))
             if os.path.exists(c)]
    assert found, (
        "the runtime lookup found no pin in the image layout — candidates were "
        f"{Store._image_pin_candidates(str(here))}")
    assert json.loads(open(found[0]).read())["index"] == \
        json.loads(PIN.read_text())["index"]


def test_runtime_lookup_finds_nothing_when_the_pin_is_not_packaged(tmp_path):
    """The pre-fix image. Proves the layout test above is not vacuous."""
    here = _materialise_image_layout(tmp_path, include_pin=False)
    assert not [c for c in Store._image_pin_candidates(str(here))
                if os.path.exists(c)]


def test_runtime_lookup_still_resolves_in_a_repo_checkout():
    here = str(REPO / "live" / "guild" / "app")
    found = [c for c in Store._image_pin_candidates(here)
             if os.path.exists(c)]
    assert found and pathlib.Path(found[0]).resolve() == PIN.resolve()


# --------------------------------------------------------------------------
# 3. THE PIN'S CONTENT AND ITS SCOPE
# --------------------------------------------------------------------------
def test_the_committed_pin_is_the_current_checkpoint_17_840():
    d = json.loads(PIN.read_text())
    assert int(d["index"]) == 17
    assert int(d["ledger_length"]) == 840
    assert d["checkpoint"]["chain_valid"] is True
    assert d["checkpoint"]["issuer"].startswith("did:key:")


def test_the_pin_raises_the_floor_for_its_own_issuer(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILD_STORE", "json")
    for v in ("GUILD_LEDGER_FLOOR_INDEX", "GUILD_LEDGER_FLOOR_LENGTH",
              "GUILD_CANONICAL_RECOVERY"):
        monkeypatch.delenv(v, raising=False)
    pinned = json.loads(PIN.read_text())
    s = Store(path="")
    s.identity = {"did": pinned["checkpoint"]["issuer"]}
    floor = s.canonical_floor()
    assert "image_pin" in floor["sources"]
    assert floor["checkpoint_index"] >= 17
    assert floor["ledger_length"] >= 840


def test_the_pin_is_ignored_for_a_different_issuer(tmp_path, monkeypatch):
    """A fork or staging stack mints its own identity and must be unaffected —
    inheriting our floor would stop it publishing anything, ever."""
    monkeypatch.setenv("GUILD_STORE", "json")
    for v in ("GUILD_LEDGER_FLOOR_INDEX", "GUILD_LEDGER_FLOOR_LENGTH",
              "GUILD_CANONICAL_RECOVERY"):
        monkeypatch.delenv(v, raising=False)
    s = Store(path="")
    s.identity = {"did": "did:key:zSomeOtherStagingIdentity"}
    floor = s.canonical_floor()
    assert "image_pin" not in floor["sources"]
    assert floor["checkpoint_index"] == -1


def test_a_missing_or_unreadable_pin_never_blocks_boot(tmp_path, monkeypatch):
    """Best-effort by design: the pin is one of three floor sources, and a bad
    one must contribute nothing rather than take the service down."""
    monkeypatch.setattr(Store, "_image_pin_candidates",
                        staticmethod(lambda here: ["/nonexistent/pin.json"]))
    s = Store(path="")
    assert s._image_pinned_floor() == {}
    assert s.canonical_state()["ok"] is True


# --------------------------------------------------------------------------
# 4. END-TO-END: build the image from the REAL Dockerfile COPY set against the
#    REAL declared context, then run the REAL Store inside it.
# --------------------------------------------------------------------------
# Docker is not available in every environment this suite runs in, so the
# build is executed faithfully rather than shelled out: the Dockerfile's own
# COPY instructions are applied to the declared dockerContext, producing the
# same filesystem the image would have. The store is then imported FROM THAT
# TREE in a subprocess, so the pin lookup runs against the container layout
# with a real `__file__`, not a stubbed path.
def _simulate_image_build(tmp_path):
    df = DOCKERFILE.read_text().splitlines()
    ctx = [ln.split(":", 1)[1].strip()
           for ln in RENDER_YAML.read_text().splitlines()
           if ln.strip().startswith("dockerContext:")][0]
    ctx_path = (REPO / ctx).resolve()
    workdir = [ln.split()[1] for ln in df
               if ln.strip().upper().startswith("WORKDIR")][0]
    root = tmp_path / workdir.lstrip("/")
    root.mkdir(parents=True)
    for line in df:
        ln = line.strip()
        if not ln.upper().startswith("COPY"):
            continue
        parts = ln.split()[1:]
        src, dst = parts[0], parts[-1]
        s, d = ctx_path / src, root / dst.lstrip("./")
        assert s.exists(), f"COPY source missing in context: {src}"
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_dir():
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)
    return root


def test_the_built_image_resolves_the_floor_to_17_840(tmp_path):
    import subprocess
    import sys
    root = _simulate_image_build(tmp_path)
    assert (root / "docs" / "checkpoints" / "latest.json").exists(), (
        "the simulated image does not contain the pin")
    code = (
        "import json,os,sys;sys.path.insert(0,'.');"
        "os.environ.update(GUILD_DATA='',GUILD_STORE='json',"
        "GUILD_ALLOW_WEAK_KDF='1',GUILD_BOOTSTRAP_EVAL='0',"
        "GUILD_ABUSE_CONTROLS='0');"
        "[os.environ.pop(k,None) for k in ('GUILD_LEDGER_FLOOR_INDEX',"
        "'GUILD_LEDGER_FLOOR_LENGTH','GUILD_CANONICAL_RECOVERY')];"
        "from app.store import Store;"
        "pin=json.load(open('docs/checkpoints/latest.json'));"
        "s=Store(path='');s.identity={'did':pin['checkpoint']['issuer']};"
        "print(json.dumps(s.canonical_floor()))"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=root,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]
    floor = json.loads(r.stdout.strip().splitlines()[-1])
    assert floor["sources"] == ["image_pin"], floor
    assert floor["checkpoint_index"] == 17
    assert floor["ledger_length"] == 840
