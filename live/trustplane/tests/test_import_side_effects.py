"""Importing the package must not touch the network, the filesystem outside
the interpreter, or the environment. Run in a subprocess with sockets and
file creation trapped."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import agentguild_trustplane

# wherever the package under test lives (repo checkout or an installed wheel)
_PKG_PARENT = str(Path(agentguild_trustplane.__file__).resolve().parent.parent)
_ENV = {**os.environ, "PYTHONPATH": _PKG_PARENT}

PROBE = r'''
import builtins, json, os, socket, sys
opened = []
_open = builtins.open
def guard_open(f, mode="r", *a, **k):
    if any(ch in mode for ch in "wax+"):
        opened.append(str(f))
    return _open(f, mode, *a, **k)
builtins.open = guard_open
def boom(*a, **k):
    raise AssertionError("network access at import time")
socket.socket.connect = boom
socket.create_connection = boom
socket.getaddrinfo = boom
env_before = dict(os.environ)
import agentguild_trustplane
from agentguild_trustplane import client, cache, gateway, outcomes, policy, engine, contract, verify
import agentguild_trustplane.integrations.pins
print(json.dumps({"opened": opened, "env_changed": env_before != dict(os.environ),
                  "version": agentguild_trustplane.__version__,
                  "modules": sorted(m for m in sys.modules if m.startswith("agentguild_trustplane"))}))
'''


def test_import_has_no_side_effects(tmp_path):
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=tmp_path, env=_ENV,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["opened"] == []
    assert res["env_changed"] is False
    assert res["version"] == agentguild_version()
    assert not list(tmp_path.iterdir())


def agentguild_version() -> str:
    from agentguild_trustplane import __version__
    return __version__


def test_optional_extras_are_not_imported_by_core():
    out = subprocess.run([sys.executable, "-c",
                          "import sys, agentguild_trustplane; "
                          "print(sorted(m for m in sys.modules if m.split('.')[0] in "
                          "('fastapi','uvicorn','mcp','crewai','langchain_core','langgraph','agents','pydantic')))"],
                         env=_ENV, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"
