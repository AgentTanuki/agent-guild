"""The canonical collaboration ledger — the moat primitive.

Locks the architecture's load-bearing guarantees: append-only hash-chaining is
tamper-evident; provenance classes weight evidence by verifiability; challenges
downweight; checkpoints are Guild-signed and detect tampering; and reputation is
a reproducible pure derivation of the immutable records.
"""
import os

os.environ["GUILD_DATA"] = ""  # in-memory only

from app.store import Store  # noqa: E402
from app.bootstrap_eval import seed_bootstrap_evaluation  # noqa: E402
from app.ledger import Ledger, CollaborationRecord, PROVENANCE_WEIGHT  # noqa: E402


def _seeded_ledger():
    s = Store(path="")
    seed_bootstrap_evaluation(s)
    return s, Ledger.from_store(s)


def test_projection_is_nonempty_and_chain_verifies():
    s, led = _seeded_ledger()
    assert led.stats()["records"] > 0
    assert led.verify_chain() is True
    # every record carries a provenance class and a hash-chained link
    prev = "0" * 64
    for r in led.records:
        assert r.provenance in PROVENANCE_WEIGHT
        assert r.prev_hash == prev
        assert r.id.startswith("vcr_")
        prev = r.hash


def test_tampering_breaks_the_chain():
    _, led = _seeded_ledger()
    assert led.verify_chain() is True
    # forge any field on an early record (guaranteed-new value)
    led.records[0].payment = led.records[0].payment + 1.0
    assert led.verify_chain() is False  # hash no longer matches body


def test_provenance_weighting_orders_evidence_strength():
    # guild_mediated must outweigh verifiable_outcome > mutual_attestation > import
    assert (PROVENANCE_WEIGHT["guild_mediated"]
            > PROVENANCE_WEIGHT["verifiable_outcome"]
            > PROVENANCE_WEIGHT["mutual_attestation"]
            > PROVENANCE_WEIGHT["external_import"])


def test_challenge_is_appended_and_downweights_target():
    s, led = _seeded_ledger()
    target = led.records[0]
    before = target.weight()
    ch = led.challenge(target.id, challenger_did="did:key:zChallenger",
                       grounds="fabricated outcome", stake=5.0)
    assert ch.id.startswith("chl_")
    assert target.challenge_status == "open"
    assert target.weight() < before  # open challenge reduces the signal
    assert led.stats()["open_challenges"] == 1


def test_signed_checkpoint_verifies_and_detects_tampering():
    s, led = _seeded_ledger()
    gid = s.guild_identity()
    cp = led.signed_checkpoint(gid["did"], gid["private_key"])
    assert Ledger.verify_checkpoint(cp) is True
    # any change to the committed state invalidates the signature
    cp["merkle_root"] = "deadbeef"
    assert Ledger.verify_checkpoint(cp) is False


def test_reputation_is_a_reproducible_derivation():
    s, led = _seeded_ledger()
    rep1 = led.derive_reputation()
    rep2 = Ledger.from_store(s).derive_reputation()
    assert rep1 == rep2  # pure function of the immutable ledger
    # higher-true-quality workers show a higher verifiable success rate
    rates = {}
    for wid, a in rep1.items():
        q = (s.get_agent(wid).get("metadata") or {}).get("true_quality")
        if q is not None and a["verifiable_success_rate"] is not None:
            rates[q] = a["verifiable_success_rate"]
    assert rates[max(rates)] > rates[min(rates)]


def test_only_verifiable_records_are_projected():
    """A task with no content-addressed deliverable must NOT enter the ledger —
    the ledger is verifiable outcomes only, never bare assertions."""
    s = Store(path="")
    a = s.register_agent("Req", ["x"], metadata={})
    b = s.register_agent("Wkr", ["x"], metadata={})
    # a task with an outcome but NO deliverable_hash (unverifiable)
    t = s.create_task(a["id"], b["id"], "x", payment=0.0, metadata={})
    t["outcome"] = "accepted"  # graded but no receipt
    led = Ledger.from_store(s)
    assert all(r.task_id != t["id"] for r in led.records)
