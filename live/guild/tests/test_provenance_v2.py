"""prov-v2 production-truth invariants.

THE rule: no one-party record ever reaches `guild_mediated`, and the ledger
never claims a party signed when it didn't. The highest provenance class
requires two-party cryptographic participation, a Guild-observed bound
invocation, or independent escrow settlement — each stamped internally,
never client-suppliable.
"""
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.store import Store  # noqa: E402
from app.crypto import generate_keypair, did_from_public_key, sign_jcs  # noqa: E402
from app.ledger import Ledger, PROVENANCE_RULES_VERSION  # noqa: E402

client = TestClient(app)


def _register(name, caps, **extra):
    return client.post("/agents/register",
                       json={"name": name, "capabilities": caps, **extra}).json()


# --------------------------------------------------------------------------
# 1. A requester alone can never manufacture guild_mediated.
# --------------------------------------------------------------------------

def test_requester_cannot_manufacture_guild_mediated_via_one_call():
    req = _register("ProvHirer", ["hiring"])
    wkr = _register("ProvDoer", ["summarize"])
    r = client.post("/collaborations", headers={"X-API-Key": req["api_key"]},
                    json={"worker_id": wkr["id"], "capability": "summarize",
                          "outcome": "accepted", "rating": 1.0,
                          "deliverable": "work product"})
    assert r.status_code == 200
    body = r.json()
    assert body["provenance"] != "guild_mediated"
    assert body["ledger_record"]["signers"] == [req["did"]]


def test_client_metadata_cannot_forge_evidence_stamps():
    """The trusted stamps (receipt_auth / settlement / guild_observed_invocation)
    must be stripped from any client-supplied task metadata."""
    req = _register("ForgeHirer", ["hiring"])
    wkr = _register("ForgeDoer", ["x"])
    t = client.post("/tasks", headers={"X-API-Key": req["api_key"]},
                    json={"requester_id": req["id"], "worker_id": wkr["id"],
                          "task_type": "x",
                          "metadata": {
                              "receipt_auth": "worker_key",
                              "settlement": {"escrow_id": "esc_fake", "amount": 999},
                              "guild_observed_invocation": "oinv_fake",
                              "harmless": "kept"}})
    assert t.status_code == 200, t.text
    from app.state import store
    task = store.get_task(t.json()["id"])
    meta = task["metadata"]
    assert "receipt_auth" not in meta
    assert "settlement" not in meta
    assert "guild_observed_invocation" not in meta
    assert meta.get("harmless") == "kept"


def test_unauthenticated_receipt_plus_attestation_stays_below_guild_mediated():
    """Four-call dance where the WORKER never authenticates (self-sovereign, no
    countersignature): the requester's attestation alone must not reach the top."""
    s = Store(path="")
    a = s.register_agent("R", ["h"], metadata={})
    # self-sovereign worker: caller supplies a DID/public key, Guild holds no key
    priv, pub = generate_keypair()
    b = s.register_agent("W", ["x"], metadata={}, public_key=pub)
    t = s.create_task(a["id"], b["id"], "x")
    s.submit_receipt(t["id"], "0x" + "ab" * 32)  # receipt_auth defaults: unauthenticated
    s.add_custodial_attestation(a, b, "x", 1.0, t["id"], "prov-v2 test")
    s.ensure_ledger_backfilled()
    rec = s.append_task_to_ledger(t["id"])
    assert rec["provenance"] == "mutual_attestation"
    assert rec["signers"] == [a["did"]]


# --------------------------------------------------------------------------
# 2. The three legitimate paths to guild_mediated.
# --------------------------------------------------------------------------

def test_two_party_crypto_reaches_guild_mediated_custodial():
    """Worker submits its receipt with ITS OWN key; requester attests: two-party."""
    req = _register("TPHirer", ["hiring"])
    wkr = _register("TPDoer", ["x"])
    t = client.post("/tasks", headers={"X-API-Key": req["api_key"]},
                    json={"requester_id": req["id"], "worker_id": wkr["id"],
                          "task_type": "x"}).json()
    r = client.post(f"/tasks/{t['id']}/receipt",
                    headers={"X-API-Key": wkr["api_key"]},
                    json={"deliverable_hash": "0x" + "cd" * 32,
                          "outcome": "accepted"})
    assert r.status_code == 200, r.text
    a = client.post("/attestations", headers={"X-API-Key": req["api_key"]},
                    json={"issuer_id": req["id"], "subject_id": wkr["id"],
                          "capability": "x", "rating": 1.0, "task_id": t["id"]})
    assert a.status_code == 200, a.text
    from app.state import store
    rec = store.append_task_to_ledger(t["id"])
    assert rec["provenance"] == "guild_mediated"
    assert rec["evidence"]["basis"] == "two_party_crypto"
    assert sorted(rec["signers"]) == sorted([req["did"], wkr["did"]])


def test_self_sovereign_worker_countersignature_reaches_guild_mediated():
    priv, pub = generate_keypair()
    did = did_from_public_key(pub)
    req = _register("SSHirer", ["hiring"])
    wkr = _register("SSDoer", ["x"], public_key=pub)
    assert wkr["did"] == did
    t = client.post("/tasks", headers={"X-API-Key": req["api_key"]},
                    json={"requester_id": req["id"], "worker_id": wkr["id"],
                          "task_type": "x"}).json()
    body = {"task_id": t["id"], "deliverable_hash": "0x" + "ef" * 32,
            "outcome": "accepted"}
    sig = sign_jcs(body, priv)
    r = client.post(f"/tasks/{t['id']}/receipt",
                    json={"deliverable_hash": body["deliverable_hash"],
                          "outcome": "accepted", "receipt_signature": sig})
    assert r.status_code == 200, r.text
    a = client.post("/attestations", headers={"X-API-Key": req["api_key"]},
                    json={"issuer_id": req["id"], "subject_id": wkr["id"],
                          "capability": "x", "rating": 1.0, "task_id": t["id"]})
    assert a.status_code == 200, a.text
    from app.state import store
    rec = store.append_task_to_ledger(t["id"])
    assert rec["provenance"] == "guild_mediated"
    assert wkr["did"] in rec["signers"]
    # and a BAD signature is rejected outright
    t2 = client.post("/tasks", headers={"X-API-Key": req["api_key"]},
                     json={"requester_id": req["id"], "worker_id": wkr["id"],
                           "task_type": "x"}).json()
    bad = client.post(f"/tasks/{t2['id']}/receipt",
                      json={"deliverable_hash": "0x" + "00" * 32,
                            "outcome": "accepted", "receipt_signature": "de" * 64})
    assert bad.status_code == 400


def test_escrow_settlement_reaches_guild_mediated():
    req = _register("EscHirer", ["hiring"])
    wkr = _register("EscDoer", ["x"])
    client.post("/billing/trial", headers={"X-API-Key": req["api_key"]})
    e = client.post("/escrow", headers={"X-API-Key": req["api_key"]},
                    json={"worker_id": wkr["id"], "amount": 20, "capability": "x"})
    assert e.status_code == 200, e.text
    rel = client.post(f"/escrow/{e.json()['id']}/release",
                      headers={"X-API-Key": req["api_key"]},
                      json={"deliverable": "the goods", "rating": 1.0})
    assert rel.status_code == 200, rel.text
    from app.state import store
    task_id = rel.json().get("task_id") or store.escrows[e.json()["id"]]["task_id"]
    rec = store.ledger_record_for_task(task_id)
    assert rec is not None
    assert rec["provenance"] == "guild_mediated"
    assert rec["evidence"]["basis"] == "independent_settlement"
    assert rec["evidence"]["settlement"]["escrow_id"] == e.json()["id"]


def test_guild_observed_invocation_reaches_guild_mediated():
    s = Store(path="")
    a = s.register_agent("R", ["h"], metadata={})
    b = s.register_agent("W", ["x"], metadata={})
    b.setdefault("metadata", {})["endpoint"] = "https://worker.example/a2a"
    t = s.create_task(a["id"], b["id"], "x")
    inv = s.begin_outbound_invocation(b["id"])
    assert inv is not None
    s.submit_receipt(t["id"], "0x" + "aa" * 32, outcome="accepted")
    ok = s.complete_outbound_invocation(inv["invocation_id"], protocol_ok=True,
                                        receipt_ref=t["id"])
    assert ok
    assert s.get_task(t["id"])["metadata"]["guild_observed_invocation"] == inv["invocation_id"]
    rec = s.append_task_to_ledger(t["id"])
    assert rec["provenance"] == "guild_mediated"
    assert rec["evidence"]["basis"] == "guild_observed_invocation"


# --------------------------------------------------------------------------
# 3. Bootstrap cohort is labelled, and history is reclassified append-only.
# --------------------------------------------------------------------------

def test_bootstrap_records_classify_as_first_party_bootstrap():
    s = Store(path="")
    from app.bootstrap_eval import seed_bootstrap_evaluation
    seed_bootstrap_evaluation(s)
    s.ensure_ledger_backfilled()
    led = Ledger.from_records(s.ledger_records)
    provs = {r.provenance for r in led.collabs()}
    assert provs == {"first_party_bootstrap"}, provs


def test_reclassification_is_append_only_and_effective():
    """Simulate a legacy chain sealed under prov-v1 (one-party guild_mediated),
    then run the honesty pass: original bytes untouched, chain stays valid,
    effective provenance downgrades, reputation uses the corrected weight."""
    s = Store(path="")
    a = s.register_agent("R", ["h"], metadata={})
    b = s.register_agent("W", ["x"], metadata={})
    res = s.record_collaboration(a, b["id"], "x", "accepted", 1.0, deliverable="d")
    rec_id = res["ledger_record"]["id"]
    # forge the legacy state: rewrite the SEALED class back to guild_mediated the
    # way prov-v1 wrote it (test-only surgery to simulate a historical chain)
    for d in s.ledger_records:
        if d.get("id") == rec_id:
            d["provenance"] = "guild_mediated"
            d["signers"] = [a["did"], b["did"]]
            # re-seal so the simulated historical chain is internally consistent
            from app.ledger import CollaborationRecord
            obj = CollaborationRecord(**d)
            d["hash"] = obj.recompute_hash()
            d["id"] = "vcr_" + d["hash"][:12]
            rec_id = d["id"]
            if s.backend is not None:      # keep the simulated history authoritative
                s.backend.put_ledger(d)
    original_bytes = dict(next(d for d in s.ledger_records if d.get("id") == rec_id))

    out = s.reclassify_ledger()
    assert out["appended"] >= 1
    led = Ledger.from_records(s.ledger_records)
    rec = next(r for r in led.collabs() if r.id == rec_id)
    # original record bytes untouched
    stored = next(d for d in s.ledger_records if d.get("id") == rec_id)
    assert stored == original_bytes
    assert rec.provenance == "guild_mediated"          # sealed history preserved
    eff = led.effective_provenance(rec)
    assert eff != "guild_mediated"                      # honesty composes at read
    # idempotent per rule version
    again = s.reclassify_ledger()
    assert again["appended"] == 0
    # stats serve the EFFECTIVE view and disclose the original + correction count
    st = led.stats()
    assert st["by_provenance"].get("guild_mediated", 0) == 0
    assert st["by_provenance_original"].get("guild_mediated", 0) == 1
    assert st["reclassified_records"] >= 1
    assert st["provenance_rules_version"] == PROVENANCE_RULES_VERSION
    # reputation derives from the corrected weight
    rep = led.derive_reputation()[b["id"]]
    assert "guild_mediated" not in rep["by_provenance"]


def test_no_serving_view_reports_one_party_records_as_guild_mediated():
    """End-to-end: after a fresh one-call record, neither /ledger/stats nor
    /ledger/reputation surfaces it at the highest class."""
    req = _register("ViewHirer", ["hiring"])
    wkr = _register("ViewDoer", ["x"])
    client.post("/collaborations", headers={"X-API-Key": req["api_key"]},
                json={"worker_id": wkr["id"], "capability": "x",
                      "outcome": "accepted", "rating": 1.0, "deliverable": "d"})
    stats = client.get("/ledger/stats").json()
    reps = client.get("/ledger/reputation").json()["agents"]
    mine = [r for r in reps if r["worker_id"] == wkr["id"]]
    assert mine and "guild_mediated" not in mine[0]["by_provenance"]
