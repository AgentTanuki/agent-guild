"""Run the actual demo worker through the public API under lost acknowledgments.

All identities, work and payments here are controlled local conformance.
"""
import importlib.util
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, market
from app.store import Store


class StopLoop(BaseException):
    pass


def load_worker():
    source = Path(__file__).resolve().parents[2] / "market_clients/worker/worker.py"
    spec = importlib.util.spec_from_file_location("handoff_worker", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    store = Store(path=str(tmp_path / "guild.json"))
    requester = store.register_agent("Controlled requester", ["hiring"], {}, first_party=True)
    worker = store.register_agent("Controlled worker", ["text.stats"], {}, first_party=True)
    offer = market.create_offer(
        store, requester, worker["id"], "text.stats", 0, 3600,
        terms={"input": "A reliable handoff.\nA recoverable result."},
        requester_key=requester["api_key"])
    monkeypatch.setattr(main, "store", store)
    client = TestClient(main.app)
    state_path = tmp_path / "worker.json"
    state_path.write_text(json.dumps({
        "agent_id": worker["id"], "api_key": worker["api_key"],
        "did": worker["did"], "proven": True}))

    class Proxy:
        failure = None
        fired = False
        accepts = 0
        receipts = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            return client.get(*args, **kwargs)

        def post(self, path, **kwargs):
            if path.endswith("/accept"):
                self.accepts += 1
            if path.endswith("/receipt"):
                self.receipts += 1
                if self.failure == "receipt_rejected" and not self.fired:
                    self.fired = True
                    return httpx.Response(503, request=httpx.Request("POST", "http://test" + path))
            response = client.post(path, **kwargs)
            boundary = "accept_ack_lost" if path.endswith("/accept") else "receipt_ack_lost"
            if self.failure == boundary and not self.fired:
                self.fired = True
                raise httpx.ReadTimeout("controlled lost acknowledgment")
            return response

    proxy = Proxy()

    def restart():
        module = load_worker()
        monkeypatch.setattr(module, "STATE_PATH", str(state_path))
        monkeypatch.setattr(module, "PUBLIC_URL", "")
        monkeypatch.setattr(module, "_client", lambda: proxy)
        module._state.update(json.loads(state_path.read_text()))
        return module

    def tick(module):
        def stop(_):
            raise StopLoop()
        with monkeypatch.context() as m:
            m.setattr(module.time, "sleep", stop)
            with pytest.raises(StopLoop):
                module.work_loop()

    return store, offer, proxy, restart, tick


@pytest.mark.parametrize("failure", ["accept_ack_lost", "receipt_rejected", "receipt_ack_lost"])
def test_worker_recovers_accepted_job_after_restart(handoff, failure):
    store, offer, proxy, restart, tick = handoff
    proxy.failure = failure
    initial = restart()
    tick(initial)
    task_id = store.offers[offer["id"]]["task_id"]
    assert task_id
    # A missing or rejected receipt response must never be called delivered.
    assert not initial._state.get("delivered")
    resumed = restart()
    executions = []
    original_stats = resumed.text_stats
    resumed.text_stats = lambda text: (executions.append(text), original_stats(text))[1]
    tick(resumed)
    task = store.get_task(task_id)
    assert task["outcome"] == "delivered"
    assert task["deliverable_hash"]
    assert len(store.tasks) == 1
    assert proxy.accepts == 1
    assert len(resumed._state["delivered"]) == 1
    assert resumed._state["delivered"][0]["task_id"] == task_id
    assert len(executions) == (1 if failure == "accept_ack_lost" else 0)
    payload = base64.b64decode(task["deliverable_url"].split(",", 1)[1])
    assert "0x" + hashlib.sha256(payload).hexdigest() == task["deliverable_hash"]
    output = json.loads(payload)
    assert output["words"] == 6 and output["lines"] == 2 and output["unique_words"] == 5
    tick(restart())
    assert proxy.receipts == (2 if failure == "receipt_rejected" else 1)


def test_recovery_survives_guild_store_reload(handoff, monkeypatch):
    store, offer, proxy, restart, tick = handoff
    proxy.failure = "accept_ack_lost"
    tick(restart())
    restored = Store(path=store.path)
    monkeypatch.setattr(main, "store", restored)
    tick(restart())
    task_id = restored.offers[offer["id"]]["task_id"]
    assert restored.tasks[task_id]["outcome"] == "delivered"
    assert len(restored.tasks) == 1 and proxy.accepts == 1


def test_storage_failure_prevents_acceptance(handoff, monkeypatch):
    store, offer, proxy, restart, _ = handoff
    worker = restart()
    def unavailable():
        raise OSError("controlled storage failure")
    monkeypatch.setattr(worker, "_save_state", unavailable)
    with pytest.raises(OSError):
        worker.poll_once(proxy)
    with pytest.raises(OSError):
        worker.poll_once(proxy)  # same process must not bypass the failed save
    assert proxy.accepts == 0 and not store.tasks
    assert store.offers[offer["id"]]["status"] == "open"


def test_failed_atomic_replace_preserves_prior_journal(handoff, monkeypatch):
    _, _, _, restart, _ = handoff
    worker = restart()
    original = Path(worker.STATE_PATH).read_bytes()
    worker._state["pending"] = {"test-offer": {"offer_hash": "hash"}}
    def unavailable(*args):
        raise OSError("controlled replacement failure")
    monkeypatch.setattr(worker.os, "replace", unavailable)
    with pytest.raises(OSError):
        worker._save_state()
    assert Path(worker.STATE_PATH).read_bytes() == original
    assert not list(Path(worker.STATE_PATH).parent.glob(".worker-*"))


def test_corrupt_journal_does_not_register_another_identity(handoff, monkeypatch):
    _, _, _, restart, _ = handoff
    worker = restart()
    Path(worker.STATE_PATH).write_text("{broken")
    worker._state.clear()
    monkeypatch.setattr(worker, "_client", lambda: pytest.fail("must not contact Guild"))
    with pytest.raises(ValueError):
        worker.ensure_identity()


def test_expired_pending_job_does_not_execute(handoff):
    store, offer, proxy, restart, tick = handoff
    proxy.failure = "accept_ack_lost"
    tick(restart())
    # Leave the signed core untouched: advance the worker's clock only.
    worker = restart()
    class FutureClock:
        fromisoformat = staticmethod(datetime.fromisoformat)
        @staticmethod
        def now(tz):
            return datetime.now(tz) + timedelta(days=2)
    worker.datetime = FutureClock
    worker.text_stats = lambda text: pytest.fail("expired work must not run")
    tick(worker)
    assert proxy.receipts == 0
    assert not worker._state["pending"]
    assert not worker._state.get("delivered")


def test_conflicting_result_is_never_overwritten(handoff):
    store, offer, proxy, restart, tick = handoff
    proxy.failure = "receipt_rejected"
    tick(restart())
    task_id = store.offers[offer["id"]]["task_id"]
    store.submit_receipt(task_id, "0x" + "aa" * 32, "data:,different", receipt_auth="worker_key")
    worker = restart()
    tick(worker)
    assert store.tasks[task_id]["deliverable_hash"] == "0x" + "aa" * 32
    assert proxy.receipts == 1 and not worker._state.get("delivered")
    assert offer["id"] in worker._state["pending"]


def test_pending_task_binding_must_stay_the_same(handoff):
    store, offer, proxy, restart, tick = handoff
    proxy.failure = "receipt_rejected"
    tick(restart())
    offer["task_id"] = "task-different"
    worker = restart()
    tick(worker)
    assert proxy.receipts == 1 and not worker._state.get("delivered")
    assert offer["id"] in worker._state["pending"]


def test_new_demo_identity_is_explicitly_owned_and_state_private(handoff):
    store, _, _, restart, _ = handoff
    worker = restart()
    Path(worker.STATE_PATH).unlink()
    worker._state.clear()
    worker.ensure_identity()
    assert store.get_agent(worker._state["agent_id"])["first_party"] is True
    assert Path(worker.STATE_PATH).stat().st_mode & 0o777 == 0o600


def test_worker_does_not_accept_another_capability(handoff):
    store, offer, proxy, restart, tick = handoff
    offer["core"]["capability"] = "code-review"
    tick(restart())
    assert proxy.accepts == 0 and not store.tasks
