"""Regression coverage for the 2026-08-27 coordination hardening.

Pins honest neutral outcomes, non-overclaiming evidence labels, and the rule
that an adjudicated fault remains negative even when the original outcome was
neutral.
"""
from __future__ import annotations

import os

os.environ["GUILD_DATA"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app import evidence_semantics  # noqa: E402
from app.crypto import generate_keypair, sign_jcs  # noqa: E402
from app.ledger import CollaborationRecord, Ledger, GENESIS  # noqa: E402
from app.main import app  # noqa: E402
from app.store import Store  # noqa: E402


client = TestClient(app)


def _register(name: str, capabilities=None, public_key=None):
    body = {"name": name, "capabilities": capabilities or [], "metadata": {}}
    if public_key:
        body["public_key"] = public_key
    r = client.post("/agents/register", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_worker_can_report_honest_infeasibility_without_fake_deliverable():
    requester = _register("neutral-requester")
    worker = _register("neutral-worker", ["neutral-cap"])
    task = client.post(
        "/tasks", headers={"X-API-Key": requester["api_key"]},
        json={"requester_id": requester["id"], "worker_id": worker["id"],
              "task_type": "neutral-cap"},
    )
    assert task.status_code == 200, task.text
    task_id = task.json()["id"]
    out = client.post(
        f"/tasks/{task_id}/receipt",
        headers={"X-API-Key": worker["api_key"]},
        json={"outcome": "infeasible"},
    )
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["deliverable_hash"] is None
    assert body["outcome"] == "infeasible"
    sem = body["evidence_semantics"]
    assert sem["contract"] == "AGOE-1/1.0"
    assert sem["label"] == "participant_claim"
    assert sem["outcome"]["reputation_effect"] == "neutral"
    assert sem["outcome"]["scoreable"] is False


def test_graded_outcome_still_requires_a_content_hash():
    requester = _register("graded-requester")
    worker = _register("graded-worker", ["graded-cap"])
    task = client.post(
        "/tasks", headers={"X-API-Key": requester["api_key"]},
        json={"requester_id": requester["id"], "worker_id": worker["id"],
              "task_type": "graded-cap"},
    ).json()
    out = client.post(
        f"/tasks/{task['id']}/receipt",
        headers={"X-API-Key": worker["api_key"]},
        json={"outcome": "accepted"},
    )
    assert out.status_code == 400
    assert "deliverable_hash" in out.text

    delivered = client.post(
        f"/tasks/{task['id']}/receipt",
        headers={"X-API-Key": worker["api_key"]},
        json={"outcome": "delivered"},
    )
    assert delivered.status_code == 400
    assert "deliverable_hash" in delivered.text


def test_worker_cannot_whitewash_a_terminal_grade_with_neutral_outcome():
    requester = _register("terminal-requester")
    worker = _register("terminal-worker", ["terminal-cap"])
    task = client.post(
        "/tasks", headers={"X-API-Key": requester["api_key"]},
        json={"requester_id": requester["id"], "worker_id": worker["id"],
              "task_type": "terminal-cap"},
    ).json()
    rejected = client.post(
        f"/tasks/{task['id']}/receipt",
        headers={"X-API-Key": requester["api_key"]},
        json={"outcome": "rejected", "deliverable_hash": "0xrejected"},
    )
    assert rejected.status_code == 200, rejected.text
    whitewash = client.post(
        f"/tasks/{task['id']}/receipt",
        headers={"X-API-Key": worker["api_key"]},
        json={"outcome": "blocked", "reason_code": "external_dependency"},
    )
    assert whitewash.status_code == 400
    assert "terminal" in whitewash.text
    current = client.get(f"/tasks/{task['id']}").json()
    assert current["outcome"] == "rejected"
    assert current["deliverable_hash"] == "0xrejected"


def test_self_sovereign_worker_must_sign_neutral_state_change():
    requester = _register("ss-neutral-requester")
    private_key, public_key = generate_keypair()
    worker = _register("ss-neutral-worker", ["ss-neutral"], public_key)
    task = client.post(
        "/tasks", headers={"X-API-Key": requester["api_key"]},
        json={"requester_id": requester["id"], "worker_id": worker["id"],
              "task_type": "ss-neutral"},
    ).json()
    unsigned = client.post(
        f"/tasks/{task['id']}/receipt", json={"outcome": "declined"})
    assert unsigned.status_code == 400
    core = {"contract": "AGTR-1/1.0", "task_id": task["id"],
            "deliverable_hash": None, "outcome": "declined",
            "reason_code": "scope_mismatch", "signer_role": "worker"}
    signed = client.post(
        f"/tasks/{task['id']}/receipt",
        json={"outcome": "declined", "reason_code": "scope_mismatch",
              "receipt_signature_contract": "AGTR-1/1.0",
              "receipt_signature": sign_jcs(core, private_key)},
    )
    assert signed.status_code == 200, signed.text
    assert signed.json()["evidence_semantics"]["outcome"]["reputation_effect"] == "neutral"

    other = client.post(
        "/tasks", headers={"X-API-Key": requester["api_key"]},
        json={"requester_id": requester["id"], "worker_id": worker["id"],
              "task_type": "ss-neutral"},
    ).json()
    tampered_core = dict(core, task_id=other["id"])
    tampered = client.post(
        f"/tasks/{other['id']}/receipt",
        json={"outcome": "declined", "reason_code": "safety_boundary",
              "receipt_signature_contract": "AGTR-1/1.0",
              "receipt_signature": sign_jcs(tampered_core, private_key)},
    )
    assert tampered.status_code == 400

    delivery = client.post(
        "/tasks", headers={"X-API-Key": requester["api_key"]},
        json={"requester_id": requester["id"], "worker_id": worker["id"],
              "task_type": "ss-neutral"},
    ).json()
    unsigned_delivery = client.post(
        f"/tasks/{delivery['id']}/receipt",
        json={"outcome": "delivered", "deliverable_hash": "0xdelivery"},
    )
    assert unsigned_delivery.status_code == 400
    delivery_core = {"task_id": delivery["id"],
                     "deliverable_hash": "0xdelivery", "outcome": "delivered"}
    signed_delivery = client.post(
        f"/tasks/{delivery['id']}/receipt",
        json={"outcome": "delivered", "deliverable_hash": "0xdelivery",
              "receipt_signature": sign_jcs(delivery_core, private_key)},
    )
    assert signed_delivery.status_code == 200, signed_delivery.text


def test_neutral_history_is_retained_but_excluded_from_reputation():
    s = Store(path="")
    requester = s.register_agent("neutral-R", [], metadata={})
    worker = s.register_agent("neutral-W", ["x"], metadata={})

    accepted = s.create_task(requester["id"], worker["id"], "x")
    s.submit_receipt(accepted["id"], "0xaccepted", outcome="delivered",
                     receipt_auth="worker_key")
    s.ensure_ledger_backfilled()
    assert s.ledger_record_for_task(accepted["id"]) is None
    s.submit_receipt(accepted["id"], "0xaccepted", outcome="accepted",
                     receipt_auth="requester")
    s.add_custodial_attestation(requester, worker, "x", 1.0,
                                accepted["id"], "accepted")
    accepted_record = s.append_task_to_ledger(accepted["id"])
    accepted_relations = {
        r["label"] for r in accepted_record["evidence"]["semantics"]["relations"]}
    assert accepted_record["evidence"]["semantics"]["label"] == "compound"
    assert accepted_relations == {"participant_claim", "bilateral_handoff"}

    stopped = s.create_task(requester["id"], worker["id"], "x")
    s.submit_receipt(stopped["id"], "0xdiagnostic", outcome="cannot_verify",
                     receipt_auth="worker_key")
    neutral_att = s.add_custodial_attestation(
        requester, worker, "x", 0.0, stopped["id"], "could not verify")
    stopped_record = s.append_task_to_ledger(stopped["id"])
    assert stopped_record is not None, "neutral history with a report hash stays auditable"
    assert stopped_record["evidence"]["semantics"]["outcome"]["scoreable"] is False
    assert s._evidence_weight(neutral_att) == 0.0
    public_neutral = s.public_attestations_for(worker["id"])[-1]
    assert public_neutral["included_in_score"] is False
    assert public_neutral["evidence_weight"] == 0.0

    derived = Ledger.from_records(s.ledger_records).derive_reputation()[worker["id"]]
    assert derived["records"] == 1
    assert derived["verifiable_success_rate"] == 1.0


def test_evidence_labels_do_not_turn_agreement_or_payment_into_truth():
    s = Store(path="")
    requester = s.register_agent("label-R", [], metadata={})
    worker = s.register_agent("label-W", ["x"], metadata={})

    one_party = s.record_collaboration(
        requester, worker["id"], "x", "accepted", 1.0, deliverable="claim")
    assert one_party["evidence_semantics"]["label"] == "participant_claim"
    assert "that execution occurred as claimed" in one_party["evidence_semantics"]["does_not_prove"]

    settled = s.record_collaboration(
        requester, worker["id"], "x", "accepted", 1.0,
        deliverable="paid claim",
        settlement={"escrow_id": "esc_test", "amount": 5})
    sem = settled["evidence_semantics"]
    assert sem["label"] == "compound"
    assert {r["label"] for r in sem["relations"]} == {
        "participant_claim", "independent_settlement"}
    assert "payment implies a correct or safe outcome" in " ".join(sem["does_not_prove"])

    tax = evidence_semantics.taxonomy()
    assert tax["independently_verified_outcome"]["availability"].startswith("reserved")


def test_guild_observed_invocation_label_is_narrow():
    s = Store(path="")
    requester = s.register_agent("observed-R", [], metadata={})
    worker = s.register_agent("observed-W", ["x"],
                              metadata={"endpoint": "https://worker.example/a2a"})
    task = s.create_task(requester["id"], worker["id"], "x")
    invocation = s.begin_outbound_invocation(worker["id"])
    assert invocation is not None
    s.submit_receipt(task["id"], "0xobserved", outcome="delivered")
    assert s.complete_outbound_invocation(
        invocation["invocation_id"], protocol_ok=True, receipt_ref=task["id"])
    record = s.append_task_to_ledger(task["id"])
    sem = evidence_semantics.for_record(record)
    assert sem["label"] == "compound"
    assert {r["label"] for r in sem["relations"]} == {
        "unattributed_claim", "guild_observed_invocation"}
    assert "the worker's hidden execution path or causal reasoning" in sem["does_not_prove"]


def test_worker_grade_is_retained_but_never_scored_as_requester_acceptance():
    s = Store(path="")
    requester = s.register_agent("role-R", [], metadata={})
    worker = s.register_agent("role-W", ["x"], metadata={})
    task = s.create_task(requester["id"], worker["id"], "x")
    s.submit_receipt(task["id"], "0xworker-grade", outcome="accepted",
                     receipt_auth="worker_key")
    rec = s.append_task_to_ledger(task["id"])
    sem = evidence_semantics.for_record(rec)
    assert sem["outcome"]["reported_effect"] == "positive"
    assert sem["outcome"]["reputation_effect"] == "none"
    assert sem["outcome"]["scoreable"] is False
    assert Ledger.from_records(s.ledger_records).derive_reputation() == {}
    evaluation = s.evaluation()
    assert evaluation["n_recommended"] + evaluation["n_baseline"] == 0


def test_neutral_receipt_event_has_safe_readback_semantics():
    s = Store(path="")
    requester = s.register_agent("event-R", [], metadata={})
    worker = s.register_agent("event-W", ["x"], metadata={})
    task = s.create_task(requester["id"], worker["id"], "x")
    s.submit_receipt(task["id"], None, outcome="blocked",
                     receipt_auth="worker_key", reason_code="external_dependency")
    event = next(e for e in reversed(s.ledger_records)
                 if e.get("type") == "receipt"
                 and e.get("body", {}).get("task_id") == task["id"])
    sem = evidence_semantics.for_record(event)
    assert sem["outcome"]["outcome"] == "blocked"
    assert sem["outcome"]["reason_code"] == "external_dependency"
    assert sem["outcome"]["scoreable"] is False


def test_reason_code_is_rejected_outside_honest_stop_outcomes():
    requester = _register("reason-requester")
    worker = _register("reason-worker", ["reason-cap"])
    task = client.post(
        "/tasks", headers={"X-API-Key": requester["api_key"]},
        json={"requester_id": requester["id"], "worker_id": worker["id"],
              "task_type": "reason-cap"},
    ).json()
    out = client.post(
        f"/tasks/{task['id']}/receipt",
        headers={"X-API-Key": requester["api_key"]},
        json={"outcome": "accepted", "deliverable_hash": "0xgrade",
              "reason_code": "safety_boundary"},
    )
    assert out.status_code == 400


def test_seed_label_cannot_restore_scoring_to_a_worker_self_grade():
    s = Store(path="")
    requester = s.register_agent("seed-role-R", [], metadata={})
    worker = s.register_agent("seed-role-W", ["x"], metadata={})
    task = s.create_task(requester["id"], worker["id"], "x",
                         metadata={"seed_supply": True})
    s.submit_receipt(task["id"], "0xseed-worker-grade", outcome="accepted",
                     receipt_auth="worker_key")
    record = s.append_task_to_ledger(task["id"])
    assert record["provenance"] == "first_party_bootstrap"
    assert record["evidence"]["outcome_claimant_role"] == "worker"
    assert Ledger.from_records(s.ledger_records).derive_reputation() == {}


def test_no_hash_stop_auth_does_not_attach_to_requester_supplied_content():
    s = Store(path="")
    requester = s.register_agent("composition-R", [], metadata={})
    worker = s.register_agent("composition-W", ["x"], metadata={})
    task = s.create_task(requester["id"], worker["id"], "x")
    s.submit_receipt(task["id"], None, outcome="blocked",
                     receipt_auth="worker_key", reason_code="external_dependency")
    s.submit_receipt(task["id"], "0xrequester-only", outcome="accepted",
                     receipt_auth="requester")
    record = s.append_task_to_ledger(task["id"])
    semantics = evidence_semantics.for_record(record)
    assert record["provenance"] == "one_party_claim"
    assert record["signers"] == [requester["did"]]
    assert "bilateral_handoff" not in {
        relation["label"] for relation in semantics["relations"]}


def test_signature_contract_metadata_requires_a_verified_signature():
    requester = _register("contract-requester")
    worker = _register("contract-worker", ["contract-cap"])
    task = client.post(
        "/tasks", headers={"X-API-Key": requester["api_key"]},
        json={"requester_id": requester["id"], "worker_id": worker["id"],
              "task_type": "contract-cap"},
    ).json()
    out = client.post(
        f"/tasks/{task['id']}/receipt",
        headers={"X-API-Key": worker["api_key"]},
        json={"outcome": "delivered", "deliverable_hash": "0xcontent",
              "receipt_signature_contract": "AGTR-1/1.0"},
    )
    assert out.status_code == 400


def test_legacy_delivered_worker_auth_migrates_before_requester_grade():
    s = Store(path="")
    requester = s.register_agent("legacy-R", [], metadata={})
    worker = s.register_agent("legacy-W", ["x"], metadata={})
    task = s.create_task(requester["id"], worker["id"], "x")
    # Exact transitional shape served by 2.5.40: the hash and worker auth were
    # stored, but the split worker_receipt_* fields did not exist yet.
    stored = s.tasks[task["id"]]
    stored["deliverable_hash"] = "0xlegacy-delivery"
    stored["outcome"] = "delivered"
    stored["metadata"]["receipt_auth"] = "worker_key"
    if s.backend is not None:
        s._persist_task(stored)

    s.submit_receipt(task["id"], "0xlegacy-delivery", outcome="accepted",
                     receipt_auth="requester")
    record = s.append_task_to_ledger(task["id"])
    assert record["provenance"] == "guild_mediated"
    assert record["evidence"]["basis"] == "two_party_crypto"
    assert set(record["signers"]) == {requester["did"], worker["did"]}


def test_upheld_challenge_overrides_neutral_outcome():
    rec = CollaborationRecord(
        seq=1, requester_did="did:key:r", worker_did="did:key:w",
        requester_id="r", worker_id="w", capability="x", task_id="t",
        outcome="infeasible", deliverable_hash="0xh", payment=0, stake=0,
        provenance="one_party_claim", signers=[], evidence={}, created_at="now",
        prev_hash=GENESIS, challenge_status="upheld",
    )
    assert rec.success() == 0


def test_unknown_outcome_fails_closed_in_interpretation():
    rec = CollaborationRecord(
        seq=1, requester_did="did:key:r", worker_did="did:key:w",
        requester_id="r", worker_id="w", capability="x", task_id="t-unknown",
        outcome="invented_escape_hatch", deliverable_hash="0xh", payment=0,
        stake=0, provenance="one_party_claim", signers=[], evidence={},
        created_at="now", prev_hash=GENESIS,
    )
    try:
        rec.success()
    except ValueError as exc:
        assert "unknown task outcome" in str(exc)
    else:
        raise AssertionError("unknown outcome was silently treated as neutral")
