"""AGD-1 source-separated evidence freshness.

Machine feedback exposed the unsafe alternative: one new liveness check or
attestation must not refresh an old competence outcome, and time must not erase
an upheld fraud finding.  These tests pin the additive machine contract.
"""
from datetime import datetime, timedelta, timezone

from app.store import Store


OLD = "2020-01-01T00:00:00+00:00"


def _store_with_old_competence_and_new_other_signals():
    store = Store(path="")
    requester = store.register_agent("freshness-requester", ["hiring"], {})
    worker = store.register_agent("freshness-worker", ["review"], {
        "endpoint": "https://worker.example/a2a",
    })
    task = store.create_task(requester["id"], worker["id"], "review")
    store.submit_receipt(task["id"], "0x" + "ab" * 32,
                         outcome="accepted", receipt_auth="worker_key")
    # Trusted internal invocation metadata, not the worker's assertion, is
    # what independently establishes capability liveness.
    store.tasks[task["id"]]["metadata"][
        "guild_observed_invocation"] = "oinv_freshness_test"
    # The competence evidence is deliberately old.
    store.tasks[task["id"]]["delivered_at"] = OLD
    store.tasks[task["id"]]["metadata"]["guild_observed_at"] = OLD

    # A current attestation is a separate advisory clock.
    store.add_custodial_attestation(
        requester, worker, "review", 0.9, task["id"], "current review")
    # Sealing after both parties participated creates the qualifying bilateral
    # competence outcome, while retaining its old delivery timestamp.
    record = store.append_task_to_ledger(task["id"])
    assert record and record["evidence"]["basis"] == "two_party_crypto"
    store.publish_checkpoint()

    # A current terminal reachability observation is a separate liveness clock.
    now = datetime.now(timezone.utc).isoformat()
    store.agents[worker["id"]]["reachability"] = {
        "status": "recently_reachable",
        "checked_at": now,
        "endpoint_fingerprint": "sha256:test",
    }
    return store, worker


def test_fresh_signals_do_not_refresh_old_competence():
    store, worker = _store_with_old_competence_and_new_other_signals()
    out = store.evidence_freshness(worker["id"])

    assert out["contract"] == "AGD-1/freshness-1"
    assert out["as_of"]
    assert out["mode"] == "source_separated"
    assert out["global_clock"] is None
    assert out["numeric_cadence_seconds"] is None

    classes = out["classes"]
    competence = classes["competence_outcomes"]
    capability = classes["capability_liveness"]
    endpoint = classes["endpoint_reachability"]
    attestations = classes["reputation_attestations"]
    assert competence["renewed_at"] == OLD
    assert competence["age_seconds"] > 365 * 86400
    assert competence["by_capability"]["review"]["latest_observed_at"] == OLD
    assert capability["age_seconds"] > 365 * 86400
    assert endpoint["age_seconds"] < 60
    assert attestations["age_seconds"] < 60
    assert "task_scoped_competence" in capability["does_not_renew"]
    assert "task_scoped_competence" in endpoint["does_not_renew"]
    assert "task_scoped_competence" in attestations["does_not_renew"]


def test_one_party_outcome_does_not_renew_competence():
    store = Store(path="")
    requester = store.register_agent("one-party-requester", ["hiring"], {})
    worker = store.register_agent("one-party-worker", ["review"], {})
    store.record_collaboration(
        requester, worker["id"], "review", "accepted", 0.9,
        deliverable="one party says this happened")

    out = store.evidence_freshness(worker["id"])["classes"]
    assert out["competence_outcomes"]["evidence_count"] == 0
    assert out["reputation_attestations"]["evidence_count"] == 1


def test_worker_self_signed_receipt_does_not_renew_capability_liveness():
    store = Store(path="")
    requester = store.register_agent("self-sign-requester", ["hiring"], {})
    worker = store.register_agent("self-sign-worker", ["review"], {})
    task = store.create_task(requester["id"], worker["id"], "review")
    store.submit_receipt(task["id"], "0x" + "11" * 32,
                         outcome="accepted", receipt_auth="worker_key")

    clock = store.evidence_freshness(worker["id"])["classes"][
        "capability_liveness"]
    assert clock["evidence_count"] == 0


def test_worker_replay_cannot_refresh_prior_guild_observation():
    store, worker = _store_with_old_competence_and_new_other_signals()
    task = next(t for t in store.tasks.values()
                if t["worker_agent_id"] == worker["id"])
    store.submit_receipt(task["id"], task["deliverable_hash"],
                         outcome="accepted", receipt_auth="worker_key")

    clock = store.evidence_freshness(worker["id"])["classes"][
        "capability_liveness"]
    assert clock["latest_observed_at"] == OLD
    assert clock["age_seconds"] > 365 * 86400


def test_capability_b_cannot_refresh_capability_a():
    store, worker = _store_with_old_competence_and_new_other_signals()
    requester = next(a for a in store.agents.values()
                     if a["name"] == "freshness-requester")
    task = store.create_task(requester["id"], worker["id"], "translate")
    store.submit_receipt(task["id"], "0x" + "cd" * 32,
                         outcome="accepted", receipt_auth="worker_key")
    store.add_custodial_attestation(
        requester, worker, "translate", 1.0, task["id"], "current translation")
    store.append_task_to_ledger(task["id"])
    store.publish_checkpoint()

    clocks = store.evidence_freshness(worker["id"])["classes"][
        "competence_outcomes"]
    assert clocks["by_capability"]["review"]["latest_observed_at"] == OLD
    assert clocks["by_capability"]["translate"]["age_seconds"] < 60
    assert clocks["age_seconds"] < 60  # aggregate is informational only

    review_prov = store.provenance_summary(worker["id"], capability="review")
    translate_prov = store.provenance_summary(
        worker["id"], capability="translate")
    assert review_prov["capability_scope"] == "review"
    assert translate_prov["capability_scope"] == "translate"
    assert review_prov["counts"] == {"guild_mediated": 1}
    assert translate_prov["counts"] == {"guild_mediated": 1}


def test_upheld_fraud_has_no_decay_or_renewal_clock():
    store, worker = _store_with_old_competence_and_new_other_signals()
    record = next(d for d in store.ledger_records
                  if d.get("worker_id") == worker["id"])
    record["challenge_status"] = "upheld"
    classes = store.evidence_freshness(
        worker["id"], committed_ledger_length=len(store.ledger_records))[
            "classes"]
    fraud = classes["upheld_fraud"]
    assert fraud["evidence_count"] == 1
    assert classes["competence_outcomes"]["evidence_count"] == 0
    assert fraud["freshness_state"] == "persistent"
    assert fraud["decays_with_time"] is False
    assert fraud["renewed_at"] is None
    assert fraud["supersession_rule"] == "explicit_superseding_adjudication_only"


def test_risk_and_check_carry_same_freshness_contract():
    store, worker = _store_with_old_competence_and_new_other_signals()
    risk = store.risk_for(worker["id"])
    check = store.check("review")
    assert risk is not None
    assert risk["freshness"]["mode"] == "source_separated"
    assert check["decision"]["freshness"]["contract"] == "AGD-1/freshness-1"
    assert check["decision"]["freshness"]["global_clock"] is None


def test_uncommitted_outcome_cannot_refresh_committed_clock():
    store, worker = _store_with_old_competence_and_new_other_signals()
    requester = next(a for a in store.agents.values()
                     if a["name"] == "freshness-requester")
    task = store.create_task(requester["id"], worker["id"], "review")
    store.submit_receipt(task["id"], "0x" + "22" * 32,
                         outcome="accepted", receipt_auth="worker_key")
    store.tasks[task["id"]]["metadata"][
        "guild_observed_invocation"] = "oinv_uncommitted_test"
    store.tasks[task["id"]]["metadata"][
        "guild_observed_at"] = datetime.now(timezone.utc).isoformat()
    store.add_custodial_attestation(
        requester, worker, "review", 1.0, task["id"], "new but uncommitted")
    store.append_task_to_ledger(task["id"])

    competence = store.evidence_freshness(worker["id"])["classes"][
        "competence_outcomes"]
    assert competence["evidence_count"] == 1
    assert competence["latest_observed_at"] == OLD


def test_future_timestamp_is_unknown_not_fresh():
    store, worker = _store_with_old_competence_and_new_other_signals()
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    store.agents[worker["id"]]["reachability"]["checked_at"] = future

    clock = store.evidence_freshness(worker["id"])["classes"][
        "endpoint_reachability"]
    assert clock["freshness_state"] == "future_timestamp"
    assert clock["age_seconds"] is None


def test_capability_label_and_group_count_are_bounded():
    store = Store(path="")
    worker = store.register_agent("bounded-worker", [], {})
    for i in range(105):
        store.attestations.append({
            "subject_id": worker["id"], "issuer_id": f"issuer-{i}",
            "capability": f"{i}-" + "x" * 180, "verified": True,
            "created_at": OLD,
        })

    clock = store.evidence_freshness(worker["id"])["classes"][
        "reputation_attestations"]
    assert len(clock["by_capability"]) == 100
    assert clock["capability_scopes_omitted"] == 5
    assert max(map(len, clock["by_capability"])) <= 128


def test_empty_contract_matches_after_sqlite_restart(monkeypatch, tmp_path):
    json_store = Store(path="")
    json_agent = json_store.register_agent("json-self-claim", ["review"], {})
    expected = json_store.evidence_freshness(json_agent["id"])

    sqlite_path = tmp_path / "freshness.sqlite3"
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    monkeypatch.setenv("GUILD_STORE_PATH", str(sqlite_path))
    sqlite_store = Store(path="")
    sqlite_agent = sqlite_store.register_agent(
        "sqlite-self-claim", ["review"], {})
    before = sqlite_store.evidence_freshness(sqlite_agent["id"])
    restarted = Store(path="")
    after = restarted.evidence_freshness(sqlite_agent["id"])

    without_as_of = lambda value: {
        key: item for key, item in value.items() if key != "as_of"}
    assert without_as_of(before) == without_as_of(expected)
    assert without_as_of(after) == without_as_of(expected)
    assert after["classes"]["competence_outcomes"]["evidence_count"] == 0
    assert after["classes"]["capability_liveness"]["evidence_count"] == 0


def test_passport_carries_signed_source_separated_freshness():
    store, worker = _store_with_old_competence_and_new_other_signals()
    passport = store.issue_passport(worker["id"])
    assert passport is not None
    freshness = passport["credentialSubject"]["freshness"]
    assert freshness["contract"] == "AGD-1/freshness-1"
    assert freshness["classes"]["competence_outcomes"]["renewed_at"] == OLD

    from app.vc import verify_credential
    assert verify_credential(passport)
    passport["credentialSubject"]["freshness"]["global_clock"] = "blended"
    assert not verify_credential(passport)


def test_passport_anchor_never_counts_uncommitted_collaboration():
    store, worker = _store_with_old_competence_and_new_other_signals()
    requester = next(a for a in store.agents.values()
                     if a["name"] == "freshness-requester")
    task = store.create_task(requester["id"], worker["id"], "review")
    store.submit_receipt(task["id"], "0x" + "33" * 32,
                         outcome="accepted", receipt_auth="worker_key")
    store.add_custodial_attestation(
        requester, worker, "review", 1.0, task["id"], "second record")
    store.append_task_to_ledger(task["id"])

    passport = store.issue_passport(worker["id"])
    assert passport is not None
    subject = passport["credentialSubject"]
    anchor = subject["ledger_anchor"]
    assert anchor["verifiable_collaborations"] == 2
    assert anchor["checkpoint"]["count"] == len(store.ledger_records)
    second = store.ledger_record_for_task(task["id"])
    proof = store.ledger_inclusion_proof(
        second["id"], checkpoint_index=anchor["checkpoint_index"])
    assert proof["record"]["id"] == second["id"]
    assert subject["freshness"]["classes"]["competence_outcomes"][
        "evidence_count"] == 2


def test_passport_refuses_when_no_checkpoint_can_be_published(monkeypatch):
    store = Store(path="")
    worker = store.register_agent("unanchored-worker", ["review"], {})

    def fail_publish():
        raise RuntimeError("checkpoint unavailable")

    monkeypatch.setattr(store, "publish_checkpoint", fail_publish)
    assert store.issue_passport(worker["id"]) is None
