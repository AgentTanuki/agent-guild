"""Requester-owned retry IDs prevent duplicated commitments after a lost reply."""
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app import main, market
from app.store import Store


@pytest.fixture
def submission(tmp_path, monkeypatch):
    store = Store(path=str(tmp_path / "guild.json"))
    requester = store.register_agent("Owned requester", ["hiring"], {}, first_party=True)
    worker = store.register_agent("Owned worker", ["text.stats"], {}, first_party=True)
    monkeypatch.setattr(main, "store", store)
    client = TestClient(main.app)
    headers = {"X-API-Key": requester["api_key"]}
    body = {"worker_id": worker["id"], "capability": "text.stats", "amount": 5,
            "deadline_seconds": 3600, "terms": {"input": "Recover this job."},
            "request_id": "controlled-job-0001"}
    return store, requester, worker, client, headers, body


def balance(store, requester):
    return store.accounts[store._account_key(requester["api_key"])]["balance"]


def test_lost_submission_reply_replays_one_offer_and_hold_after_reload(submission, monkeypatch):
    store, requester, _, client, headers, body = submission
    before = balance(store, requester)
    first = client.post("/offers", headers=headers, json=body)
    assert first.status_code == 200
    # The requester did not retain this response. Its original request ID and
    # exact intent survived; retry after the Guild process reloads its storage.
    restored = Store(path=store.path)
    monkeypatch.setattr(main, "store", restored)
    replay = client.post("/offers", headers=headers, json=body)
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["core"] == first.json()["core"]
    assert len(restored.offers) == len(restored.escrows) == 1
    assert balance(restored, requester) == before - 5
    assert sum(row["type"] == "escrow_hold" for row in restored.billing_log) == 1


def test_reused_request_id_cannot_change_the_commitment(submission):
    store, requester, _, client, headers, body = submission
    before = balance(store, requester)
    assert client.post("/offers", headers=headers, json=body).status_code == 200
    changed = {**body, "amount": 9}
    response = client.post("/offers", headers=headers, json=changed)
    assert response.status_code == 409
    assert len(store.offers) == len(store.escrows) == 1
    assert balance(store, requester) == before - 5


def test_concurrent_retries_share_one_commitment(submission):
    store, requester, _, client, headers, body = submission
    before = balance(store, requester)
    with ThreadPoolExecutor(max_workers=6) as pool:
        replies = list(pool.map(lambda _: client.post("/offers", headers=headers, json=body), range(6)))
    assert all(r.status_code == 200 for r in replies)
    assert len({r.json()["id"] for r in replies}) == 1
    assert len(store.escrows) == 1 and balance(store, requester) == before - 5


@pytest.mark.parametrize("field,value", [
    ("worker_id", "another-worker"), ("capability", "other-capability"),
    ("deadline_seconds", 4000), ("terms", {"input": "Different work."})])
def test_every_material_term_is_bound(submission, field, value):
    store, _, _, client, headers, body = submission
    first = client.post("/offers", headers=headers, json=body)
    changed = client.post("/offers", headers=headers, json={**body, field: value})
    assert first.status_code == 200 and changed.status_code == 409
    assert len(store.offers) == len(store.escrows) == 1


def test_retry_id_is_scoped_to_authenticated_requester(submission):
    store, _, _, client, headers, body = submission
    first = client.post("/offers", headers=headers, json=body)
    other = store.register_agent("Another owned requester", ["hiring"], {}, first_party=True)
    second = client.post("/offers", headers={"X-API-Key": other["api_key"]}, json=body)
    assert second.status_code == 200 and first.json()["id"] != second.json()["id"]
    assert second.json()["core"]["requester_id"] == other["id"]
    assert len(store.offers) == len(store.escrows) == 2
    assert client.post("/offers", json=body).status_code == 401


def test_replay_does_not_renew_expired_deadline_or_fund_again(submission):
    store, requester, _, client, headers, body = submission
    first = client.post("/offers", headers=headers, json=body).json()
    store.offers[first["id"]]["status"] = "expired"
    if store.backend:
        store._persist_kv("offers", store.offers)
    store._save()
    before = balance(store, requester)
    replay = client.post("/offers", headers=headers, json=body).json()
    assert replay["status"] == "expired" and replay["core"] == first["core"]
    assert balance(store, requester) == before and len(store.escrows) == 1


def test_replay_returns_accepted_task_without_resetting_it(submission):
    store, _, worker, client, headers, body = submission
    first = client.post("/offers", headers=headers, json=body).json()
    accepted = market.accept_offer(store, first["id"], worker)
    replay = client.post("/offers", headers=headers, json=body).json()
    assert replay["task_id"] == accepted["task_id"] and replay["status"] == "accepted"
    assert len(store.tasks) == len(store.escrows) == 1


def test_legacy_requests_remain_distinct(submission):
    store, _, _, client, headers, body = submission
    body.pop("request_id")
    first = client.post("/offers", headers=headers, json=body).json()
    second = client.post("/offers", headers=headers, json=body).json()
    assert first["id"] != second["id"] and len(store.escrows) == 2


@pytest.mark.parametrize("retry_id", ["", "short", "x" * 129, "job secret 123", "å" * 10, 12345678])
def test_invalid_retry_ids_do_not_mutate(submission, retry_id):
    store, _, _, client, headers, body = submission
    r = client.post("/offers", headers=headers, json={**body, "request_id": retry_id})
    assert r.status_code == 422 and not store.offers and not store.escrows


@pytest.mark.parametrize("amount", [0.5, 5.5])
def test_fractional_signed_amount_cannot_disagree_with_integer_hold(submission, amount):
    store, _, _, client, headers, body = submission
    r = client.post("/offers", headers=headers, json={**body, "amount": amount})
    assert r.status_code == 400 and not store.offers and not store.escrows


def test_request_fingerprint_is_signed_and_raw_retry_id_is_not_published(submission):
    from app.crypto import verify_jcs, public_key_from_did
    _, requester, _, client, headers, body = submission
    offer = client.post("/offers", headers=headers, json=body).json()
    assert offer["core"]["request_fingerprint"]
    assert verify_jcs(offer["core"], offer["offer_sig"], public_key_from_did(requester["did"]))
    assert body["request_id"] not in json.dumps(offer)


def test_failure_after_hold_rolls_back_and_same_process_can_retry(submission, monkeypatch):
    store, requester, _, client, headers, body = submission
    before = balance(store, requester)
    ledger_before = len(store.ledger_records)
    append = store.append_ledger_event

    def fail_final(type_, *args, **kwargs):
        if type_ == "task_created":
            raise OSError("controlled failure after hold and before final offer commit")
        return append(type_, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(store, "append_ledger_event", fail_final)
        with pytest.raises(OSError):
            client.post("/offers", headers=headers, json=body)
    assert not store.offers and not store.escrows
    assert balance(store, requester) == before
    assert len(store.ledger_records) == ledger_before
    restored = Store(path=store.path)
    assert not restored.offers and not restored.escrows
    assert balance(restored, requester) == before
    assert len(restored.ledger_records) == ledger_before
    assert client.post("/offers", headers=headers, json=body).status_code == 200
    assert len(store.offers) == len(store.escrows) == 1
    assert balance(store, requester) == before - 5


def test_actual_process_exit_before_commit_leaves_no_orphan_hold(submission, tmp_path):
    store, requester, worker, _, _, body = submission
    before = balance(store, requester)
    spec = tmp_path / "controlled-submission.json"
    spec.write_text(json.dumps({"path": store.path, "requester_id": requester["id"],
                                "key": requester["api_key"], "request": body}))
    spec.chmod(0o600)
    code = '''
import json, os, sys
from app.store import Store
from app import market
data = json.load(open(sys.argv[1]))
s = Store(path=data['path'])
original = s._save
def crash():
    if s.offers:
        os._exit(23)
    return original()
s._save = crash
r = data['request']
market.create_offer(s, s.get_agent(data['requester_id']), r['worker_id'],
                    r['capability'], r['amount'], r['deadline_seconds'],
                    terms=r['terms'], requester_key=data['key'], request_id=r['request_id'])
'''
    proc = subprocess.run([sys.executable, "-c", code, str(spec)], capture_output=True, timeout=30)
    assert proc.returncode == 23, proc.stderr.decode()
    restored = Store(path=store.path)
    assert not restored.offers and not restored.escrows
    assert balance(restored, requester) == before
    offer = market.create_offer(restored, requester, worker["id"], body["capability"],
                                body["amount"], body["deadline_seconds"], terms=body["terms"],
                                requester_key=requester["api_key"], request_id=body["request_id"])
    assert offer["id"] and len(restored.escrows) == 1
    assert balance(restored, requester) == before - 5


def test_distinct_sqlite_connections_do_not_duplicate_or_overwrite_offers(submission):
    store, requester, worker, _, _, body = submission
    if store.backend is None:
        pytest.skip("Independent server connections require the SQLite production store")
    second = Store(path=store.path)
    before = balance(store, requester)

    def submit(args):
        target, retry_id = args
        return market.create_offer(target, requester, worker["id"], body["capability"],
                                   body["amount"], body["deadline_seconds"], terms=body["terms"],
                                   requester_key=requester["api_key"], request_id=retry_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        same = list(pool.map(submit, [(store, body["request_id"]), (second, body["request_id"])]))
        distinct = list(pool.map(submit, [(store, "another-job-0002"), (second, "another-job-0003")]))
    assert same[0]["id"] == same[1]["id"]
    assert distinct[0]["id"] != distinct[1]["id"]
    restored = Store(path=store.path)
    assert len(restored.offers) == len(restored.escrows) == 3
    assert balance(restored, requester) == before - 15
