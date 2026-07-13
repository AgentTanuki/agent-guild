"""Pytest entry point for AGI-1 conformance against ANY issuer:

    pytest conformance/ --issuer-base=https://agent-guild-5d5r.onrender.com \
                        --capability=hello

Default issuer-base: a LOCAL Guild instance is booted (production FastAPI
app, GUILD_STORE=json, temp dir) and seeded, so `pytest conformance/` works
offline out of the box; pass --issuer-base to run the same suite against the
live service.
"""
from __future__ import annotations

import json
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                      # agentguild_trustplane
sys.path.insert(0, str(HERE.parent.parent / "guild"))     # app (local issuer)


def pytest_addoption(parser):
    parser.addoption("--issuer-base", action="store", default=None,
                     help="base URL of the issuer under test "
                          "(default: boot a local Guild)")
    parser.addoption("--capability", action="store", default=None,
                     help="capability to request a signed decision for "
                          "(default: seeded local capability / 'hello' live)")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="session")
def issuer(request):
    base = request.config.getoption("--issuer-base")
    capability = request.config.getoption("--capability")
    if base:
        return {"base": base.rstrip("/"),
                "capability": capability or "hello", "local": False}
    # boot a local issuer (the production app) and seed evidence
    import os
    import tempfile
    import threading
    import time
    os.environ["GUILD_DATA"] = ""
    os.environ.setdefault("GUILD_STORE", "json")
    os.environ["GUILD_DATA_DIR"] = tempfile.mkdtemp(prefix="conf_guild_")
    from app.main import app as guild_app
    import uvicorn
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(guild_app, host="127.0.0.1",
                                           port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"

    def post(path, body, key=None):
        req = urllib.request.Request(
            base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     **({"X-API-Key": key} if key else {})}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())

    w = post("/agents/register", {"name": "conf-worker",
                                  "capabilities": ["conf-cap"],
                                  "metadata": {}})
    r = post("/agents/register", {"name": "conf-req", "capabilities": []})
    for i in range(3):
        post("/collaborations", {"worker_id": w["id"],
                                 "capability": "conf-cap",
                                 "outcome": "accepted", "rating": 0.9,
                                 "deliverable": f"d{i}"}, key=r["api_key"])
    return {"base": base, "capability": capability or "conf-cap",
            "local": True, "worker": w}


@pytest.fixture(scope="session")
def fetch(issuer):
    def _fetch(path):
        with urllib.request.urlopen(issuer["base"] + path, timeout=30) as r:
            return json.loads(r.read().decode())
    return _fetch


@pytest.fixture(scope="session")
def signed_decision(issuer, fetch):
    q = urllib.parse.urlencode({"capability": issuer["capability"],
                                "signed": "true"})
    return fetch(f"/check?{q}")
