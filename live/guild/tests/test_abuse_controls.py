"""Abuse controls: registration flooding, trial-credit farming, unfunded
expensive-read bursts, and storage exhaustion are all bounded (app/abuse.py).
The rest of the suite runs with GUILD_ABUSE_CONTROLS=0; these tests flip the
switch and drive each limiter over its configured budget."""
import os

os.environ["GUILD_DATA"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import abuse  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _controls_on(monkeypatch):
    monkeypatch.setenv("GUILD_ABUSE_CONTROLS", "1")
    abuse.reset()
    yield
    abuse.reset()


def test_registration_flooding_is_rate_limited(monkeypatch):
    monkeypatch.setenv("GUILD_RL_REGISTER", "3")
    codes = [client.post("/agents/register",
                         json={"name": f"Flood{i}", "capabilities": ["x"]}
                         ).status_code for i in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == codes[4] == 429
    body = client.post("/agents/register",
                       json={"name": "F", "capabilities": ["x"]}).json()
    assert body["detail"]["error"] == "rate_limited"
    assert body["detail"]["bucket"] == "register"
    assert body["detail"]["retry_after_seconds"] >= 1


def test_trial_credit_farming_is_rate_limited(monkeypatch):
    monkeypatch.setenv("GUILD_RL_TRIAL", "2")
    codes = [client.post("/billing/trial").status_code for i in range(4)]
    assert codes.count(429) == 2


def test_unfunded_read_bursts_are_bounded(monkeypatch):
    monkeypatch.setenv("GUILD_RL_READ_BURST", "4")
    codes = [client.get("/check?capability=x").status_code for _ in range(6)]
    assert codes[-1] == 429
    # a funded/keyed caller is NOT subject to the unfunded budget
    abuse.reset()
    codes = [client.get("/check?capability=x",
                        headers={"X-API-Key": "sk_nonexistent"}).status_code
             for _ in range(6)]
    assert 429 not in codes


def test_oversized_bodies_are_rejected():
    r = client.post("/collaborations",
                    content=b"x" * 10,
                    headers={"Content-Length": str(abuse.MAX_BODY_BYTES + 1),
                             "Content-Type": "application/json"})
    assert r.status_code == 413


def test_oversized_deliverables_are_rejected(monkeypatch):
    monkeypatch.setenv("GUILD_ABUSE_CONTROLS", "0")   # isolate the store-level cap
    from app.store import Store
    s = Store(path="")
    a = s.register_agent("R", ["h"], metadata={})
    b = s.register_agent("W", ["x"], metadata={})
    with pytest.raises(ValueError):
        s.record_collaboration(a, b["id"], "x", "accepted", 1.0,
                               deliverable="d" * (65536 + 1))
    with pytest.raises(ValueError):
        s.record_collaboration(a, b["id"], "x", "accepted", 1.0,
                               deliverable_hash="0x" + "a" * 300)


def test_limits_do_not_apply_when_disabled(monkeypatch):
    monkeypatch.setenv("GUILD_ABUSE_CONTROLS", "0")
    monkeypatch.setenv("GUILD_RL_REGISTER", "1")
    codes = [client.post("/agents/register",
                         json={"name": f"Off{i}", "capabilities": ["x"]}
                         ).status_code for i in range(3)]
    assert 429 not in codes
