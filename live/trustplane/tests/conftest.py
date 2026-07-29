"""Shared fixtures: a REAL local Guild (uvicorn over loopback) with seeded,
honestly-earned evidence, plus a Gateway pointed at it.

The Guild server here is the production FastAPI app from live/guild —
not a stub — running with GUILD_STORE=json in a temp dir. Loopback SSRF
screening is relaxed IN THIS LAB ONLY so workers on 127.0.0.1 can be
endpoint-verified; every other code path is production code.
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                      # agentguild_trustplane
sys.path.insert(0, str(HERE.parent.parent / "guild"))     # app (guild server)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="session")
def guild_server():
    os.environ["GUILD_STORE"] = "json"
    os.environ["GUILD_DATA_DIR"] = tempfile.mkdtemp(prefix="tp_guild_")
    import app.reachability as reach
    # lab affordance: allow loopback probes (production screens them out)
    reach._screen_ip = lambda ip: (True, "ok (lab)")
    from app.main import app as guild_app, store
    import uvicorn
    port = _free_port()
    config = uvicorn.Config(guild_app, host="127.0.0.1", port=port,
                            log_level="error")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield {"base": f"http://127.0.0.1:{port}", "store": store, "port": port}
    server.should_exit = True


@pytest.fixture()
def gateway(guild_server, tmp_path):
    from agentguild_trustplane.gateway import Gateway
    from agentguild_trustplane.policy import RiskPolicy
    return Gateway(policy=RiskPolicy(), state_dir=tmp_path / "state",
                   base_url=guild_server["base"])


@pytest.fixture(scope="session")
def seeded(guild_server):
    """Register a requester + a worker with earned evidence; return ids/keys."""
    import json as _json
    import urllib.request

    def call(method, path, body=None, key=None):
        req = urllib.request.Request(
            guild_server["base"] + path,
            data=_json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json",
                     **({"X-API-Key": key} if key else {})},
            method=method)
        with urllib.request.urlopen(req, timeout=15) as r:
            return _json.loads(r.read().decode())

    w = call("POST", "/agents/register",
             {"name": "tp-worker", "capabilities": ["tp-echo"],
              "metadata": {"endpoint": "https://example.com/a2a"}})
    r = call("POST", "/agents/register", {"name": "tp-requester",
                                          "capabilities": []})
    for i in range(4):
        call("POST", "/collaborations", key=r["api_key"], body={
            "worker_id": w["id"], "capability": "tp-echo",
            "outcome": "accepted", "rating": 0.9, "deliverable": f"d{i}"})
    return {"worker": w, "requester": r, "call": call}
