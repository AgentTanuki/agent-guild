"""Clean-environment x402 v2 interoperability harness.

Boots TWO real HTTP servers on localhost:
  * the Agent Guild service (x402 enabled, billing enforced), and
  * the deterministic fake facilitator (fake_facilitator.py) that
    cryptographically verifies EIP-3009 authorizations.

The test client is the OFFICIAL x402 SDK client with a real EVM signer —
no Guild-specific client code touches the payment path.

This suite is intentionally OUTSIDE tests/ so the main suite never needs the
EVM dependencies; CI runs it in its own clean venv (ci.yml → x402-interop).
"""
from __future__ import annotations

import os
import pathlib
import socket
import sys
import threading
import time

# --- environment BEFORE the app imports -------------------------------------
GUILD_PAY_TO = "0x1111111111111111111111111111111111111111"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


GUILD_PORT = _free_port()
FACILITATOR_PORT = _free_port()
GUILD_BASE = f"http://127.0.0.1:{GUILD_PORT}"
FACILITATOR_BASE = f"http://127.0.0.1:{FACILITATOR_PORT}"

os.environ["GUILD_DATA"] = ""                      # in-memory store
os.environ["GUILD_BOOTSTRAP_EVAL"] = "0"
os.environ["GUILD_ABUSE_CONTROLS"] = "0"
os.environ["GUILD_ALLOW_WEAK_KDF"] = "1"
os.environ["GUILD_BILLING_ENFORCED"] = "1"
os.environ["GUILD_X402_ENABLED"] = "1"
os.environ["GUILD_X402_PAY_TO"] = GUILD_PAY_TO
os.environ["GUILD_X402_FACILITATOR"] = FACILITATOR_BASE
os.environ["GUILD_PUBLIC_HOST"] = GUILD_BASE

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest          # noqa: E402
import uvicorn         # noqa: E402


class _ServerThread:
    def __init__(self, asgi_app, port: int):
        self._server = uvicorn.Server(uvicorn.Config(
            asgi_app, host="127.0.0.1", port=port, log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.time() + 30
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("server failed to start")
            time.sleep(0.05)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture(scope="session")
def live_stack():
    from fake_facilitator import app as facilitator_app
    from app.main import app as guild_app

    facilitator = _ServerThread(facilitator_app, FACILITATOR_PORT)
    guild = _ServerThread(guild_app, GUILD_PORT)
    facilitator.start()
    guild.start()
    yield {"guild": GUILD_BASE, "facilitator": FACILITATOR_BASE,
           "pay_to": GUILD_PAY_TO}
    guild.stop()
    facilitator.stop()
