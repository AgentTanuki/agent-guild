"""Cold-instance stale-head regression coverage (incidents 2026-07-31, 08-01).

WHAT ACTUALLY BROKE, twice
--------------------------
The first ``POST /ledger/checkpoint/publish`` after a cold start returned
checkpoint index 14 / ledger_length 834 while the authoritative feed head was
17 / 840, then immediate retries returned the correct head. The first fix
compared the DURABLE view against the PROCESS'S in-memory view and refused when
durable was behind. That guard cannot see this bug: on a cold boot both views
are hydrated from the same stale source, so they agree — and two copies of the
same stale state agreeing is not evidence of freshness.

These tests reproduce the real shape of the incident deterministically:

  * ``test_cold_instance_seeded_from_stale_snapshot_*`` — an EMPTY sqlite
    database (fresh disk / volume that had not attached yet) next to the older
    JSON snapshot baked into the image. This is a REACHABLE PATH that
    reproduces the symptom; it is NOT an established production root cause.
    What we observed in production was the symptom only.
  * ``test_publish_refuses_below_operator_pin`` — the same regression expressed
    through the operator pin, for both backends.
  * ``test_idempotent_return_is_the_head_not_the_last_element`` — the exact
    payload production returned: a superseded entry handed back as canonical
    because the feed was read in insertion order rather than by index.
  * ``test_concurrent_publishers_*`` — two threads publishing at once must
    produce ONE new index and never two entries claiming the same index.
  * ``test_floor_is_issuer_scoped`` — a fork/fresh deployment with its own
    identity must NOT inherit our production floor, or it could never publish.

Every assertion is on OBSERVABLE STORE BEHAVIOUR (what publish returns / what
lands in the feed), not on internal helper return values, because the previous
round of hardening passed its unit tests while production stayed broken.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading

import pytest

from app.store import (  # noqa: E402
    CanonicalFloorRegressionError,
    MalformedCheckpointEntryError,
    CanonicalWriteRefused as _CWR,
    CanonicalWriteRefused,
    Store,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _seed_real_feed(path, n_publishes=3, records_per=2):
    """Build a genuine store with a real published feed via the SAME code path
    production uses (registration -> evidence on the durable chain -> publish),
    so the fixture cannot pass while the real write path is broken."""
    st = Store(path)
    n = 0
    for i in range(n_publishes):
        for j in range(records_per):
            n += 1
            st.register_agent(name=f"seed-{i}-{j}", capabilities=["fact-check"],
                              metadata={})
        st.publish_checkpoint()
    return st


def _feed_indices(st):
    return sorted(int(e["index"]) for e in st.checkpoints)


# --------------------------------------------------------------------------
# 1. A REACHABLE PATH TO THE OBSERVED DEFECT — cold sqlite seeded from a stale
#    JSON snapshot. Not claimed as the production root cause: we observed the
#    symptom (a superseded checkpoint returned after a cold start) and this is
#    the path that reproduces it deterministically.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["sqlite", "json"])
def test_cold_instance_seeded_from_stale_snapshot_refuses_to_publish(
        tmp_path, monkeypatch, mode):
    """A process hydrated from a snapshot OLDER than a position already proven
    published must refuse every canonical write — not silently publish from it.

    This reproduces the incident's SHAPE: the stale view is internally
    consistent, so nothing relative can detect it. Only a floor external to the
    state under test can."""
    data = tmp_path / "guild.json"

    # (a) the real deployment publishes a feed and records its high-water mark.
    monkeypatch.setenv("GUILD_STORE", "json")
    live = _seed_real_feed(str(data), n_publishes=3)
    real_head = max(_feed_indices(live))
    real_len = len(live.ledger_records)
    assert real_head >= 2, "fixture must build a multi-entry feed"

    # (b) the image carries an OLD snapshot: rewind the file to an earlier
    #     state, exactly as a container image built days ago would.
    snap = json.loads(data.read_text())
    stale_cutoff = 1                      # keep only checkpoints 0..1
    stale_cps = [c for c in snap["checkpoints"]
                 if int(c["index"]) <= stale_cutoff]
    snap["checkpoints"] = stale_cps
    snap["ledger_records"] = snap["ledger_records"][
        :int(stale_cps[-1]["ledger_length"])]
    # the stale snapshot ALSO carries the stale hwm — the attacker-free but
    # equally dangerous case where every local source agrees.
    snap["canonical_hwm"] = {"checkpoint_index": stale_cutoff,
                             "ledger_length": stale_cps[-1]["ledger_length"]}
    data.write_text(json.dumps(snap))

    # (c) the FLOOR survives independently of that snapshot (operator pin
    #     stands in for the image pin, which is issuer-scoped to production).
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_INDEX", str(real_head))
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_LENGTH", str(real_len))
    monkeypatch.setenv("GUILD_STORE", mode)
    if mode == "sqlite":
        # fresh/empty database next to the stale JSON = the cutover branch.
        monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "cold.sqlite3"))

    cold = Store(str(data))

    # It boots (a dead instance would be worse) but it KNOWS it is behind...
    state = cold.canonical_state()
    assert state["ok"] is False
    assert state["served_checkpoint_index"] < state["floor_checkpoint_index"]
    assert "STALE CANONICAL VIEW" in state["warning"]

    # ...and it refuses to make a canonical commitment.
    with pytest.raises(CanonicalFloorRegressionError):
        cold.publish_checkpoint()

    # The refusal is durable: retrying does not eventually succeed into a fork.
    with pytest.raises(CanonicalFloorRegressionError):
        cold.publish_checkpoint()

    # And nothing was appended to the feed.
    assert max(_feed_indices(cold)) == stale_cutoff


def test_healthy_json_instance_at_or_above_floor_still_publishes(
        tmp_path, monkeypatch):
    """The floor must not become a foot-gun: a CURRENT instance publishes
    normally. A guard that also blocks the good path is not a fix."""
    monkeypatch.setenv("GUILD_STORE", "json")
    monkeypatch.delenv("GUILD_CANONICAL_RECOVERY", raising=False)
    data = tmp_path / "guild.json"
    live = _seed_real_feed(str(data), n_publishes=2)
    head, length = max(_feed_indices(live)), len(live.ledger_records)

    monkeypatch.setenv("GUILD_LEDGER_FLOOR_INDEX", str(head))
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_LENGTH", str(length))

    warm = Store(str(data))
    assert warm.canonical_state()["ok"] is True
    # idempotent: nothing new landed, so we get the CURRENT head back
    assert int(warm.publish_checkpoint()["index"]) == head
    # and a real append still publishes forward
    warm.register_agent(name="fresh-1", capabilities=["x"], metadata={})
    assert int(warm.publish_checkpoint()["index"]) == head + 1


def test_healthy_warm_sqlite_instance_still_publishes(tmp_path, monkeypatch):
    """The sqlite good path is a NON-EMPTY backend — a real attached database.

    Note what this test is NOT: an empty backend seeded from JSON. That path is
    now degraded by design (see section 5), because an absent persistent disk
    must not become an automatic cutover. The healthy sqlite case is the one
    where the database is actually there."""
    monkeypatch.delenv("GUILD_LEDGER_FLOOR_INDEX", raising=False)
    monkeypatch.delenv("GUILD_LEDGER_FLOOR_LENGTH", raising=False)
    monkeypatch.delenv("GUILD_CANONICAL_RECOVERY", raising=False)
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    db = str(tmp_path / "warm.sqlite3")
    monkeypatch.setenv("GUILD_STORE_PATH", db)

    # build a genuine sqlite-backed feed (fresh issuer, no cutover involved)
    first = _seed_real_feed(str(tmp_path / "warm.json"), n_publishes=2)
    head, length = max(_feed_indices(first)), len(first.ledger_records)
    assert first.canonical_state()["ok"] is True

    # reopen the SAME database: backend is non-empty, so no cutover branch
    reopened = Store(str(tmp_path / "warm.json"))
    assert reopened.canonical_state()["ok"] is True
    assert max(_feed_indices(reopened)) == head
    assert int(reopened.publish_checkpoint()["index"]) == head
    reopened.register_agent(name="fresh-2", capabilities=["x"], metadata={})
    assert int(reopened.publish_checkpoint()["index"]) == head + 1


# --------------------------------------------------------------------------
# 2. THE EXACT PAYLOAD PRODUCTION RETURNED
# --------------------------------------------------------------------------
def test_idempotent_return_is_the_head_not_the_last_element(
        tmp_path, monkeypatch):
    """Production returned index 14 while the feed head was 17 because the
    idempotency check read ``checkpoints[-1]`` — insertion order, not index
    order. A feed loaded out of order must still return its true head."""
    monkeypatch.setenv("GUILD_STORE", "json")
    data = tmp_path / "guild.json"
    st = _seed_real_feed(str(data), n_publishes=3)
    head = max(_feed_indices(st))

    # simulate an out-of-order load (what a partial/reordered hydration gives):
    # deterministically move the true head OFF the end of the list.
    st.checkpoints.insert(0, st.checkpoints.pop())
    assert int(st.checkpoints[-1]["index"]) != head, (
        "fixture must actually put a non-head entry last")

    got = st.publish_checkpoint()
    assert int(got["index"]) == head, (
        "publish handed back a superseded checkpoint as canonical")


def test_read_surface_orders_by_index_not_insertion(tmp_path, monkeypatch):
    """The feed READ path must present the true head too — a pinning third
    party reads /ledger/checkpoints, not the store."""
    monkeypatch.setenv("GUILD_STORE", "json")
    from fastapi.testclient import TestClient
    data = tmp_path / "guild.json"
    st = _seed_real_feed(str(data), n_publishes=3)
    head = max(_feed_indices(st))
    st.checkpoints.insert(0, st.checkpoints.pop())
    assert int(st.checkpoints[-1]["index"]) != head

    import app.main as main
    real = main.store
    main.store = st
    try:
        r = TestClient(main.app).get("/ledger/checkpoints?limit=5")
        assert r.status_code == 200
        assert int(r.json()["checkpoints"][0]["index"]) == head
    finally:
        main.store = real


def test_publish_over_http_fails_closed_with_409(tmp_path, monkeypatch):
    """TRANSPORT-LEVEL. Six review rounds found unit tests passing while the
    SYSTEM stayed broken, so the refusal is asserted through the real HTTP
    route the ops pass actually calls."""
    monkeypatch.setenv("GUILD_STORE", "json")
    from fastapi.testclient import TestClient
    data = tmp_path / "guild.json"
    live = _seed_real_feed(str(data), n_publishes=2)
    head, length = max(_feed_indices(live)), len(live.ledger_records)

    snap = json.loads(data.read_text())
    snap["checkpoints"] = [c for c in snap["checkpoints"]
                           if int(c["index"]) == 0]
    snap["ledger_records"] = snap["ledger_records"][
        :int(snap["checkpoints"][-1]["ledger_length"])]
    data.write_text(json.dumps(snap))

    monkeypatch.setenv("GUILD_LEDGER_FLOOR_INDEX", str(head))
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_LENGTH", str(length))
    stale = Store(str(data))

    import app.main as main
    real, real_tok = main.store, main.ADMIN_TOKEN
    main.store = stale
    main.ADMIN_TOKEN = ""            # auth is not what is under test here
    try:
        c = TestClient(main.app, raise_server_exceptions=False)
        r = c.post("/ledger/checkpoint/publish")
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "canonical_floor_regression"
        # and the read surface warns rather than serving it as canonical
        feed = c.get("/ledger/checkpoints").json()
        assert feed["status"] == "stale_canonical_view"
        assert feed["canonical_state"]["ok"] is False
        assert "do_not_pin" in feed
        assert c.get("/health").json()["canonical_state"]["ok"] is False
    finally:
        main.store, main.ADMIN_TOKEN = real, real_tok


# --------------------------------------------------------------------------
# 3. CONCURRENCY — two publishers must not fork the feed
# --------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["sqlite", "json"])
def test_concurrent_publishers_never_duplicate_an_index(
        tmp_path, monkeypatch, mode):
    monkeypatch.setenv("GUILD_STORE", mode)
    if mode == "sqlite":
        monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "conc.sqlite3"))
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=1)
    st.register_agent(name="contended", capabilities=["x"], metadata={})

    results, errors = [], []

    def go():
        try:
            results.append(st.publish_checkpoint())
        except CanonicalWriteRefused as exc:   # refusing is an ACCEPTABLE outcome
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    idxs = _feed_indices(st)
    assert len(idxs) == len(set(idxs)), f"forked feed: duplicate index in {idxs}"
    # exactly one new checkpoint for one batch of new evidence
    assert max(idxs) == 1, idxs
    assert all(int(r["index"]) <= 1 for r in results)


# --------------------------------------------------------------------------
# 4. THE FLOOR MUST NOT LEAK ACROSS DEPLOYMENTS
# --------------------------------------------------------------------------
def test_image_pin_floor_is_issuer_scoped(tmp_path, monkeypatch):
    """A fork/staging stack mints its own guild identity and legitimately
    starts from an empty feed. If our production image pin applied to it, it
    could never publish anything — the fix would have broken every other
    deployment of this code."""
    monkeypatch.setenv("GUILD_STORE", "json")
    monkeypatch.delenv("GUILD_LEDGER_FLOOR_INDEX", raising=False)
    monkeypatch.delenv("GUILD_LEDGER_FLOOR_LENGTH", raising=False)

    fresh = Store(str(tmp_path / "fork.json"))
    pin_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "..", "..", "docs", "checkpoints", "latest.json")
    if not os.path.exists(pin_path):
        pytest.skip("no image pin committed")
    pinned = json.load(open(pin_path))
    assert int(pinned["index"]) >= 0

    own_did = (fresh.identity or {}).get("did")
    assert own_did != pinned["checkpoint"]["issuer"], (
        "fixture invalid: fork must have its own identity")
    # floor ignores the foreign pin, so a brand-new deployment can publish
    assert fresh.canonical_floor()["checkpoint_index"] == -1
    first = fresh.publish_checkpoint()
    assert int(first["index"]) == 0


def test_hwm_is_monotonic_and_cannot_be_lowered(tmp_path, monkeypatch):
    """A later stale boot must not be able to lower its own floor by
    'publishing' a smaller position."""
    monkeypatch.setenv("GUILD_STORE", "json")
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=3)
    high = st.canonical_floor()["checkpoint_index"]
    assert high >= 2
    st._record_canonical_hwm({"index": 0, "ledger_length": 1,
                              "checkpoint": {"head_hash": "x"}})
    assert st.canonical_floor()["checkpoint_index"] == high


# --------------------------------------------------------------------------
# 5. THE FLOOR IS ONE CHECKPOINT BEHIND THE THREAT (Codex review 2026-08-01)
# --------------------------------------------------------------------------
# A floor comparison alone still accepts the defect one checkpoint later:
#   image pin / head = 17
#   production publishes 18, stores HWM = 18
#   the database is lost or not attached
#   -> the HWM dies WITH the database; the stale image seeds head 17; the image
#      floor is also 17; seed == floor; a "seed < floor" guard is satisfied and
#      an instance a full checkpoint behind reality publishes 18 again.
#
# So the trigger cannot be a comparison. An EMPTY durable backend being seeded
# for an issuer we can PROVE has published before is unsafe on its face: the
# only evidence that could show the seed is current is the database we just
# failed to find.
#
# SCOPE NOTE, deliberately stated. This defends the observed DEFECT CLASS —
# a canonical write built on a view that cannot be shown to be current. We have
# NOT established by direct evidence that empty-disk seeding was the production
# root cause on 2026-07-31 / 08-01; we observed the symptom (a superseded
# checkpoint returned after a cold start) and this is the reachable path that
# reproduces it. The guard is justified by the symptom, not by a proven cause.
def test_empty_backend_cutover_is_unsafe_even_when_seed_equals_the_pin(
        tmp_path, monkeypatch):
    """seed head == floor head, and it must STILL refuse."""
    monkeypatch.setenv("GUILD_STORE", "json")
    monkeypatch.delenv("GUILD_CANONICAL_RECOVERY", raising=False)
    data = tmp_path / "guild.json"
    live = _seed_real_feed(str(data), n_publishes=3)
    head, length = max(_feed_indices(live)), len(live.ledger_records)

    # the snapshot is EXACTLY at the pinned position — not behind it
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_INDEX", str(head))
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_LENGTH", str(length))
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "empty.sqlite3"))

    cold = Store(str(data))
    state = cold.canonical_state()
    assert state["ok"] is False, (
        "an empty backend seeded for an established issuer must start "
        "canonical-degraded even when its seed matches the pin")
    assert (state["seed_degraded"]["reason"]
            == "empty_backend_cutover_for_established_issuer")
    with pytest.raises(CanonicalFloorRegressionError):
        cold.publish_checkpoint()


def test_a_genuinely_fresh_issuer_still_publishes(tmp_path, monkeypatch):
    """The guard must not brick a new deployment: no prior canonical history,
    no pin that names it, so nothing to contradict."""
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "fresh.sqlite3"))
    monkeypatch.delenv("GUILD_LEDGER_FLOOR_INDEX", raising=False)
    monkeypatch.delenv("GUILD_LEDGER_FLOOR_LENGTH", raising=False)
    monkeypatch.delenv("GUILD_CANONICAL_RECOVERY", raising=False)

    fresh = Store(str(tmp_path / "brand-new.json"))
    assert fresh.canonical_state()["ok"] is True
    assert int(fresh.publish_checkpoint()["index"]) == 0


def test_recovery_authorization_must_name_this_issuer_and_this_head(
        tmp_path, monkeypatch):
    """An 'ignore the guard' flag would be set once and left on forever, then
    silently authorise every future stale boot. Naming the exact head means the
    authorization expires the moment the feed moves."""
    monkeypatch.setenv("GUILD_STORE", "json")
    data = tmp_path / "guild.json"
    live = _seed_real_feed(str(data), n_publishes=2)
    head, length = max(_feed_indices(live)), len(live.ledger_records)
    own_did = (live.identity or {}).get("did")

    monkeypatch.setenv("GUILD_LEDGER_FLOOR_INDEX", str(head))
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_LENGTH", str(length))
    monkeypatch.setenv("GUILD_STORE", "sqlite")

    # (a) wrong head -> NOT applied, and the mismatch is recorded
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "a.sqlite3"))
    monkeypatch.setenv("GUILD_CANONICAL_RECOVERY", f"{own_did}:{head + 5}")
    st = Store(str(data))
    assert st.canonical_state()["ok"] is False
    assert "NOT APPLICABLE" in st.canonical_seed_degraded[
        "recovery_authorization"]

    # (b) wrong issuer -> NOT applied
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "b.sqlite3"))
    monkeypatch.setenv("GUILD_CANONICAL_RECOVERY", f"did:key:zSomeoneElse:{head}")
    assert Store(str(data)).canonical_state()["ok"] is False

    # (c) exact issuer AND exact head -> applied, and stays VISIBLE
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "c.sqlite3"))
    monkeypatch.setenv("GUILD_CANONICAL_RECOVERY", f"{own_did}:{head}")
    ok = Store(str(data))
    state = ok.canonical_state()
    assert state["ok"] is True
    assert state["recovery_authorized"]["adopted_head"] == head
    ok.register_agent(name="post-recovery", capabilities=["x"], metadata={})
    assert int(ok.publish_checkpoint()["index"]) == head + 1


# --------------------------------------------------------------------------
# 6. THE QUARANTINE MUST SURVIVE RESTARTS (restart repro, 2026-08-01)
# --------------------------------------------------------------------------
# The previous commit held the degraded flag IN MEMORY ONLY. The cutover made
# the backend non-empty, so the very next process took the normal load branch,
# the flag came back `{}`, canonical_state reported ok, and publish succeeded.
# Measured on that commit: cold_ok=False/degraded=True, then
# restart_ok=True/degraded=False, restart_publish_index=2.
#
# A guard a restart clears is not a guard. These tests open the SAME database
# repeatedly — which is what a process restart is — and assert the refusal
# holds every time.
def _quarantined(tmp_path, monkeypatch, n_publishes=3):
    """Build a real feed, then a cold empty-sqlite cutover beside it.
    Returns (json_path, issuer_did, seed_head)."""
    monkeypatch.setenv("GUILD_STORE", "json")
    monkeypatch.delenv("GUILD_CANONICAL_RECOVERY", raising=False)
    data = tmp_path / "guild.json"
    live = _seed_real_feed(str(data), n_publishes=n_publishes)
    head, length = max(_feed_indices(live)), len(live.ledger_records)
    did = (live.identity or {}).get("did")
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_INDEX", str(head))
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_LENGTH", str(length))
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "quarantined.sqlite3"))
    return str(data), did, head


def test_quarantine_survives_repeated_restarts_on_the_same_database(
        tmp_path, monkeypatch):
    """First cutover refused; second and third Store over the SAME database
    refused. This is the exact bypass that was measured."""
    data, _did, _head = _quarantined(tmp_path, monkeypatch)

    for boot in range(1, 4):
        st = Store(data)
        assert st.canonical_state()["ok"] is False, (
            f"boot {boot}: quarantine did not survive the restart")
        assert st.canonical_seed_degraded, f"boot {boot}: flag lost"
        with pytest.raises(CanonicalFloorRegressionError):
            st.publish_checkpoint()


def test_quarantine_is_written_before_the_cutover_is_trusted(
        tmp_path, monkeypatch):
    """The quarantine lives in the DATABASE it describes, so it travels with
    that database rather than with the process that made it."""
    data, did, head = _quarantined(tmp_path, monkeypatch)
    first = Store(data)
    row = first.backend.fetch_kv(Store.CANONICAL_QUARANTINE_KEY, None)
    assert row, "quarantine was not persisted"
    assert row["issuer"] == did
    assert int(row["seed_checkpoint_index"]) == head


def test_wrong_head_or_wrong_issuer_cannot_clear_it_across_restarts(
        tmp_path, monkeypatch):
    data, did, head = _quarantined(tmp_path, monkeypatch)
    Store(data)                                   # create the quarantine

    monkeypatch.setenv("GUILD_CANONICAL_RECOVERY", f"{did}:{head + 9}")
    st = Store(data)
    assert st.canonical_state()["ok"] is False
    assert "NOT APPLICABLE" in st.canonical_seed_degraded[
        "recovery_authorization"]

    monkeypatch.setenv("GUILD_CANONICAL_RECOVERY", f"did:key:zSomeoneElse:{head}")
    assert Store(data).canonical_state()["ok"] is False

    # and after both failed attempts it is STILL refusing
    monkeypatch.delenv("GUILD_CANONICAL_RECOVERY", raising=False)
    with pytest.raises(CanonicalFloorRegressionError):
        Store(data).publish_checkpoint()


def test_exact_recovery_clears_it_durably_without_keeping_the_env_var(
        tmp_path, monkeypatch):
    """Recovery must not be a one-boot env bypass: the adoption is persisted,
    so the operator does not keep a 'skip the guard' flag set forever, and the
    decision stays auditable."""
    data, did, head = _quarantined(tmp_path, monkeypatch)
    Store(data)

    monkeypatch.setenv("GUILD_CANONICAL_RECOVERY", f"{did}:{head}")
    recovered = Store(data)
    assert recovered.canonical_state()["ok"] is True
    assert recovered.canonical_state()["recovery_authorized"][
        "adopted_head"] == head

    # env var GONE — the adoption is durable
    monkeypatch.delenv("GUILD_CANONICAL_RECOVERY", raising=False)
    for _ in range(2):
        again = Store(data)
        state = again.canonical_state()
        assert state["ok"] is True
        assert state["recovery_authorized"]["adopted_head"] == head
        assert again.backend.fetch_kv(Store.CANONICAL_QUARANTINE_KEY, None) \
            in (None, {}, [])
    again.register_agent(name="post-recovery", capabilities=["x"], metadata={})
    assert int(again.publish_checkpoint()["index"]) == head + 1


def test_a_stale_adoption_does_not_clear_a_later_quarantine(
        tmp_path, monkeypatch):
    """An adoption authorises ONE issuer at ONE head. If the feed later moves
    and a NEW quarantine appears at a different head, the old authorisation
    must not be inherited."""
    data, did, head = _quarantined(tmp_path, monkeypatch)
    Store(data)
    monkeypatch.setenv("GUILD_CANONICAL_RECOVERY", f"{did}:{head}")
    st = Store(data)
    assert st.canonical_state()["ok"] is True
    monkeypatch.delenv("GUILD_CANONICAL_RECOVERY", raising=False)

    # forge the situation the guard must survive: a quarantine at a LATER head
    # while the durable adoption still names the earlier one.
    st._persist_kv(Store.CANONICAL_QUARANTINE_KEY, {
        "at": "2026-08-01T00:00:00+00:00", "issuer": did,
        "seed_checkpoint_index": head + 1, "seed_ledger_length": 999,
        "reason": "empty_backend_cutover_for_established_issuer"})

    later = Store(data)
    state = later.canonical_state()
    assert state["ok"] is False, "a stale adoption cleared a later quarantine"
    assert "stale_adoption_refused" in later.canonical_seed_degraded
    with pytest.raises(CanonicalFloorRegressionError):
        later.publish_checkpoint()


def test_restoring_the_authoritative_database_is_clean_by_construction(
        tmp_path, monkeypatch):
    """The quarantine travels with the bad database. Pointing the instance at
    the real one needs no override — that database never carried the row."""
    data, _did, head = _quarantined(tmp_path, monkeypatch)
    Store(data)
    assert Store(data).canonical_state()["ok"] is False

    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "authoritative.sqlite3"))
    monkeypatch.delenv("GUILD_LEDGER_FLOOR_INDEX", raising=False)
    monkeypatch.delenv("GUILD_LEDGER_FLOOR_LENGTH", raising=False)
    good = Store(str(tmp_path / "authoritative.json"))
    assert good.canonical_state()["ok"] is True
    assert int(good.publish_checkpoint()["index"]) == 0


def test_a_truly_fresh_issuer_is_never_quarantined(tmp_path, monkeypatch):
    """No prior canonical history, so nothing to contradict — unaffected across
    restarts too."""
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "fresh.sqlite3"))
    for k in ("GUILD_LEDGER_FLOOR_INDEX", "GUILD_LEDGER_FLOOR_LENGTH",
              "GUILD_CANONICAL_RECOVERY"):
        monkeypatch.delenv(k, raising=False)
    data = str(tmp_path / "fresh.json")

    first = Store(data)
    assert first.canonical_state()["ok"] is True
    assert int(first.publish_checkpoint()["index"]) == 0
    second = Store(data)
    assert second.canonical_state()["ok"] is True
    assert second.backend.fetch_kv(Store.CANONICAL_QUARANTINE_KEY, None) is None


# --------------------------------------------------------------------------
# 7. UNREADABLE CANONICAL STATE MUST FAIL CLOSED (fault-injection audit)
# --------------------------------------------------------------------------
# The durable quarantine closed the restart bypass, and then the EXCEPTION PATH
# reopened it. `_rehydrate_canonical_quarantine` swallowed read failures and
# returned, with a comment asserting that leaving state untouched was safe. On
# the restart path the untouched state is `{}` — i.e. NOT degraded. Injecting a
# failure into fetch_kv(CANONICAL_QUARANTINE_KEY) alone produced ok=True,
# degraded=False and a successful publish at index 2: the same bypass as the
# memory-only bug, reached through the error path instead of a restart.
#
# If we cannot READ whether this database is quarantined, we do not know that it
# is not. Unknown is not clear.
import app.store_sqlite as _store_sqlite  # noqa: E402


@pytest.fixture
def _kv_fault(monkeypatch):
    """Inject a read failure for ONE canonical-state key, through a REOPENED
    database — i.e. the restart path, which is where the bypass lived."""
    real = _store_sqlite.SqliteBackend.fetch_kv

    def install(key):
        def broken(self, name, default=None):
            if name == key:
                raise RuntimeError(
                    "simulated read failure /srv/secret/path?token=SUPERSECRET")
            return real(self, name, default)
        monkeypatch.setattr(_store_sqlite.SqliteBackend, "fetch_kv", broken)
    return install


@pytest.mark.parametrize("key_attr", ["CANONICAL_QUARANTINE_KEY",
                                      "CANONICAL_ADOPTION_KEY",
                                      "CANONICAL_HWM_KEY"])
def test_unreadable_canonical_state_fails_closed(tmp_path, monkeypatch,
                                                 _kv_fault, key_attr):
    data, _did, _head = _quarantined(tmp_path, monkeypatch)
    Store(data)                                     # create the quarantine

    _kv_fault(getattr(Store, key_attr))
    st = Store(data)
    assert st.canonical_state()["ok"] is False, (
        f"a failed read of {key_attr} silently un-quarantined the instance")
    assert st.canonical_state_unreadable
    with pytest.raises(CanonicalFloorRegressionError):
        st.publish_checkpoint()


def test_unreadable_state_never_leaks_the_error_message(tmp_path, monkeypatch,
                                                        _kv_fault):
    """canonical_state is served publicly on /health and /ledger/checkpoints.
    An exception message can carry a path, a DSN or a token, so only the
    exception CLASS is recorded."""
    data, _did, _head = _quarantined(tmp_path, monkeypatch)
    Store(data)
    _kv_fault(Store.CANONICAL_QUARANTINE_KEY)
    st = Store(data)
    blob = json.dumps(st.canonical_state())
    assert "SUPERSECRET" not in blob
    assert "/srv/secret" not in blob
    assert st.canonical_seed_degraded["error_class"] == "RuntimeError"


def test_recovery_cannot_clear_an_unreadable_state_quarantine(
        tmp_path, monkeypatch, _kv_fault):
    """The issuer and head an authorization is matched against live in the rows
    we could not read. Authorising a state we cannot verify is not an
    authorization, it is an off switch."""
    data, did, head = _quarantined(tmp_path, monkeypatch)
    Store(data)

    _kv_fault(Store.CANONICAL_QUARANTINE_KEY)
    monkeypatch.setenv("GUILD_CANONICAL_RECOVERY", f"{did}:{head}")
    st = Store(data)
    assert st.canonical_state()["ok"] is False
    assert "REFUSED" in st.canonical_seed_degraded["recovery_authorization"]
    with pytest.raises(CanonicalFloorRegressionError):
        st.publish_checkpoint()


def test_unreadable_hwm_blocks_recovery_regardless_of_read_order(
        tmp_path, monkeypatch, _kv_fault):
    """Order-independence. The hwm is normally read later (via
    canonical_floor), which is AFTER the recovery decision — so it is probed up
    front, or an unreadable hwm would not block a recovery it should never have
    been evaluated alongside."""
    data, did, head = _quarantined(tmp_path, monkeypatch)
    Store(data)
    _kv_fault(Store.CANONICAL_HWM_KEY)
    monkeypatch.setenv("GUILD_CANONICAL_RECOVERY", f"{did}:{head}")
    st = Store(data)
    assert st.canonical_state()["ok"] is False
    assert st.canonical_state_unreadable["unreadable"] == "high_water_mark"
    # and the refusal did NOT get persisted as an adoption
    monkeypatch.delenv("GUILD_CANONICAL_RECOVERY", raising=False)
    assert Store(data).canonical_state()["ok"] is False


def test_removing_the_fault_rehydrates_the_durable_quarantine_normally(
        tmp_path, monkeypatch, _kv_fault):
    """The fail-closed path must not be a one-way door: once the state is
    readable again, the REAL quarantine comes back — not a clean slate."""
    data, _did, head = _quarantined(tmp_path, monkeypatch)
    Store(data)

    _kv_fault(Store.CANONICAL_QUARANTINE_KEY)
    faulted = Store(data)
    assert (faulted.canonical_seed_degraded["reason"]
            == "canonical_quarantine_state_unreadable")

    monkeypatch.undo()                              # fault removed
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "quarantined.sqlite3"))
    healed = Store(data)
    assert healed.canonical_state()["ok"] is False
    assert (healed.canonical_seed_degraded["reason"]
            == "empty_backend_cutover_for_established_issuer")
    assert not healed.canonical_state_unreadable
    assert int(healed.canonical_seed_degraded["seed_checkpoint_index"]) == head
    with pytest.raises(CanonicalFloorRegressionError):
        healed.publish_checkpoint()


def test_unreadable_state_fails_closed_over_http(tmp_path, monkeypatch,
                                                 _kv_fault):
    """TRANSPORT LEVEL. /health must report it and the publish route must 409 —
    a guard that only holds inside the Store is not a guard operators can see."""
    from fastapi.testclient import TestClient
    data, _did, _head = _quarantined(tmp_path, monkeypatch)
    Store(data)
    _kv_fault(Store.CANONICAL_QUARANTINE_KEY)
    st = Store(data)

    import app.main as main
    real, real_tok = main.store, main.ADMIN_TOKEN
    main.store, main.ADMIN_TOKEN = st, ""
    try:
        c = TestClient(main.app, raise_server_exceptions=False)
        health = c.get("/health").json()["canonical_state"]
        assert health["ok"] is False
        assert health["state_unreadable"]["error_class"] == "RuntimeError"
        r = c.post("/ledger/checkpoint/publish")
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "canonical_floor_regression"
        feed = c.get("/ledger/checkpoints").json()
        assert feed["status"] == "stale_canonical_view"
    finally:
        main.store, main.ADMIN_TOKEN = real, real_tok


# --------------------------------------------------------------------------
# 8. A MALFORMED OPERATOR PIN MUST NOT CRASH ANYTHING (mechanical regression)
# --------------------------------------------------------------------------
# The exception sweep that made unreadable state fail closed introduced a
# mechanical bug: `_operator_pinned_floor` was still decorated @staticmethod
# while its handler wrote `self.canonical_pin_malformed`. With
# GUILD_LEDGER_FLOOR_INDEX=not-an-int, canonical_state() raised
# `NameError: name 'self' is not defined` — so the guard crashed /health and
# publish outright. A guard that crashes the surface it protects is worse than
# the silence it replaced.
#
# A malformed pin is OPERATOR ERROR, not a storage failure. It must be visible,
# must not crash, must not leak the operator-supplied value onto a public
# surface, and must never relax a structural quarantine.
@pytest.mark.parametrize("var", ["GUILD_LEDGER_FLOOR_INDEX",
                                 "GUILD_LEDGER_FLOOR_LENGTH"])
def test_malformed_operator_pin_does_not_crash_boot_health_or_publish(
        tmp_path, monkeypatch, var):
    monkeypatch.setenv("GUILD_STORE", "json")
    monkeypatch.delenv("GUILD_LEDGER_FLOOR_INDEX", raising=False)
    monkeypatch.delenv("GUILD_LEDGER_FLOOR_LENGTH", raising=False)
    monkeypatch.delenv("GUILD_CANONICAL_RECOVERY", raising=False)
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=2)
    head = max(_feed_indices(st))

    monkeypatch.setenv(var, "not-an-int")

    # boot
    reopened = Store(str(tmp_path / "guild.json"))
    # health / read surface
    state = reopened.canonical_state()
    assert state["ok"] is True
    assert state["operator_pin_malformed"]["malformed"] == [var]
    # publish
    reopened.register_agent(name="after-bad-pin", capabilities=["x"],
                            metadata={})
    assert int(reopened.publish_checkpoint()["index"]) == head + 1


def test_malformed_pin_over_http_does_not_500(tmp_path, monkeypatch):
    """TRANSPORT LEVEL: /health and the publish route are what an operator and
    the ops pass actually touch."""
    from fastapi.testclient import TestClient
    monkeypatch.setenv("GUILD_STORE", "json")
    for v in ("GUILD_LEDGER_FLOOR_INDEX", "GUILD_LEDGER_FLOOR_LENGTH",
              "GUILD_CANONICAL_RECOVERY"):
        monkeypatch.delenv(v, raising=False)
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=2)
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_INDEX", "not-an-int")

    import app.main as main
    real, real_tok = main.store, main.ADMIN_TOKEN
    main.store, main.ADMIN_TOKEN = st, ""
    try:
        c = TestClient(main.app, raise_server_exceptions=False)
        h = c.get("/health")
        assert h.status_code == 200, h.text
        assert h.json()["canonical_state"]["ok"] is True
        assert c.get("/ledger/checkpoints").status_code == 200
        r = c.post("/ledger/checkpoint/publish")
        assert r.status_code == 200, r.text
    finally:
        main.store, main.ADMIN_TOKEN = real, real_tok


def test_malformed_pin_never_echoes_the_operator_supplied_value(
        tmp_path, monkeypatch):
    """canonical_state is public. The pin is operator-supplied input and must
    not be reflected onto it — naming the variable is enough to fix it."""
    monkeypatch.setenv("GUILD_STORE", "json")
    for v in ("GUILD_LEDGER_FLOOR_INDEX", "GUILD_LEDGER_FLOOR_LENGTH"):
        monkeypatch.delenv(v, raising=False)
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=1)
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_INDEX", "OOPS-SECRET-VALUE-123")
    blob = json.dumps(Store(str(tmp_path / "guild.json")).canonical_state())
    assert "OOPS-SECRET-VALUE-123" not in blob
    assert "GUILD_LEDGER_FLOOR_INDEX" in blob


def test_malformed_pin_does_not_clear_a_structural_quarantine(
        tmp_path, monkeypatch):
    """The quarantine is structural and does not consult the pin. A bad pin
    must not become an accidental way to relax it."""
    data, _did, _head = _quarantined(tmp_path, monkeypatch)
    Store(data)                                    # quarantined
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_INDEX", "not-an-int")
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_LENGTH", "also-bad")
    st = Store(data)
    assert st.canonical_state()["ok"] is False
    assert (st.canonical_seed_degraded["reason"]
            == "empty_backend_cutover_for_established_issuer")
    with pytest.raises(CanonicalFloorRegressionError):
        st.publish_checkpoint()


def test_one_bad_half_does_not_discard_the_good_half(tmp_path, monkeypatch):
    """Parsed separately: a bad LENGTH must not throw away a valid INDEX, or a
    typo in one variable would silently lower the floor set by the other."""
    monkeypatch.setenv("GUILD_STORE", "json")
    for v in ("GUILD_LEDGER_FLOOR_INDEX", "GUILD_LEDGER_FLOOR_LENGTH",
              "GUILD_CANONICAL_RECOVERY"):
        monkeypatch.delenv(v, raising=False)
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=3)
    head = max(_feed_indices(st))

    monkeypatch.setenv("GUILD_LEDGER_FLOOR_INDEX", str(head))
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_LENGTH", "not-an-int")
    reopened = Store(str(tmp_path / "guild.json"))
    floor = reopened.canonical_floor()
    assert floor["checkpoint_index"] == head, (
        "a malformed length discarded a valid index")
    assert "operator_pin" in floor["sources"]


def test_valid_pins_still_raise_the_floor(tmp_path, monkeypatch):
    """The positive path: a well-formed pin ABOVE the local state must still be
    enforced, or the fix would have quietly disarmed the operator override."""
    monkeypatch.setenv("GUILD_STORE", "json")
    for v in ("GUILD_LEDGER_FLOOR_INDEX", "GUILD_LEDGER_FLOOR_LENGTH",
              "GUILD_CANONICAL_RECOVERY"):
        monkeypatch.delenv(v, raising=False)
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=2)
    head, length = max(_feed_indices(st)), len(st.ledger_records)

    monkeypatch.setenv("GUILD_LEDGER_FLOOR_INDEX", str(head + 5))
    monkeypatch.setenv("GUILD_LEDGER_FLOOR_LENGTH", str(length + 50))
    pinned = Store(str(tmp_path / "guild.json"))
    floor = pinned.canonical_floor()
    assert floor["checkpoint_index"] == head + 5
    assert floor["ledger_length"] == length + 50
    assert pinned.canonical_state()["ok"] is False
    with pytest.raises(CanonicalFloorRegressionError):
        pinned.publish_checkpoint()


# --------------------------------------------------------------------------
# 9. RUNTIME (POST-BOOT) HWM READ FAILURE MUST ABORT THE WRITE
# --------------------------------------------------------------------------
# The boot-time flag check in _assert_canonical_floor cannot see an hwm read
# that starts failing AFTER boot: canonical_floor() would set the flag and
# return {}, _assert had already passed its entry check, _record_canonical_hwm
# read it again, set the flag again, and the publish still completed. Measured
# on the previous commit: healthy first publish index 0, then injecting an hwm
# read failure on the SAME live Store published index 1 with state_unreadable
# already set.
def _live_sqlite_store(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "live.sqlite3"))
    for v in ("GUILD_LEDGER_FLOOR_INDEX", "GUILD_LEDGER_FLOOR_LENGTH",
              "GUILD_CANONICAL_RECOVERY", "GUILD_CANONICAL_ACCEPT_GAPS"):
        monkeypatch.delenv(v, raising=False)
    st = Store(str(tmp_path / "live.json"))
    st.register_agent(name="seed", capabilities=["x"], metadata={})
    assert int(st.publish_checkpoint()["index"]) == 0
    return st


def _break_hwm(monkeypatch):
    real = _store_sqlite.SqliteBackend.fetch_kv

    def broken(self, name, default=None):
        if name == Store.CANONICAL_HWM_KEY:
            raise RuntimeError("hwm read down")
        return real(self, name, default)
    monkeypatch.setattr(_store_sqlite.SqliteBackend, "fetch_kv", broken)


def test_runtime_hwm_failure_aborts_the_canonical_write(tmp_path, monkeypatch):
    st = _live_sqlite_store(tmp_path, monkeypatch)
    before = sorted(int(e["index"]) for e in st.backend.all_checkpoints())

    _break_hwm(monkeypatch)
    st.register_agent(name="after", capabilities=["x"], metadata={})
    with pytest.raises(CanonicalFloorRegressionError):
        st.publish_checkpoint()

    # THE DATABASE MUST BE UNCHANGED: the strict insert has to roll back with
    # the surrounding transaction, or a refused publish still leaves a
    # committed checkpoint behind.
    assert sorted(int(e["index"])
                  for e in st.backend.all_checkpoints()) == before


def test_runtime_hwm_failure_also_blocks_the_idempotent_return(
        tmp_path, monkeypatch):
    """The no-op path is still a canonical assertion — it hands the caller an
    entry and advances the high-water mark. It must fail closed too."""
    st = _live_sqlite_store(tmp_path, monkeypatch)
    _break_hwm(monkeypatch)
    # nothing new landed, so this is the idempotent branch
    with pytest.raises(CanonicalFloorRegressionError):
        st.publish_checkpoint()


def test_runtime_hwm_failure_fails_closed_over_http(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    st = _live_sqlite_store(tmp_path, monkeypatch)
    before = sorted(int(e["index"]) for e in st.backend.all_checkpoints())
    _break_hwm(monkeypatch)
    st.register_agent(name="after", capabilities=["x"], metadata={})

    import app.main as main
    real, real_tok = main.store, main.ADMIN_TOKEN
    main.store, main.ADMIN_TOKEN = st, ""
    try:
        c = TestClient(main.app, raise_server_exceptions=False)
        r = c.post("/ledger/checkpoint/publish")
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "canonical_floor_regression"
        listed = [int(e["index"]) for e in
                  c.get("/ledger/checkpoints").json()["checkpoints"]]
        assert sorted(listed) == before
    finally:
        main.store, main.ADMIN_TOKEN = real, real_tok
    assert sorted(int(e["index"])
                  for e in st.backend.all_checkpoints()) == before


def test_removing_the_hwm_fault_permits_a_normal_publish(tmp_path, monkeypatch):
    st = _live_sqlite_store(tmp_path, monkeypatch)
    _break_hwm(monkeypatch)
    st.register_agent(name="after", capabilities=["x"], metadata={})
    with pytest.raises(CanonicalFloorRegressionError):
        st.publish_checkpoint()

    monkeypatch.undo()
    # undo() also cleared GUILD_STORE_PATH — re-point at the SAME database, or
    # this would open a fresh one and prove nothing.
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "live.sqlite3"))
    healed = Store(str(tmp_path / "live.json"))
    assert healed.canonical_state()["ok"] is True
    assert _feed_indices(healed) == [0], "the refused publish must not persist"
    assert int(healed.publish_checkpoint()["index"]) == 1


# --------------------------------------------------------------------------
# 10. THE FEED HEAD IS THE MAX INDEX, EVERYWHERE
# --------------------------------------------------------------------------
# The previous commit selected by index for the idempotent return and the next
# index, but still built prev_entry_sha256 from checkpoints[-1]. Over an
# out-of-order feed ([2, 0, 1]) a new index 3 committed to index 1, so the
# published chain silently skipped a link.
def _entry_hash(entry):
    from app.crypto import canonicalize
    return hashlib.sha256(canonicalize(entry).encode("utf-8")).hexdigest()


def test_reordered_feed_still_links_to_the_true_predecessor(
        tmp_path, monkeypatch):
    monkeypatch.setenv("GUILD_STORE", "json")
    monkeypatch.delenv("GUILD_CANONICAL_ACCEPT_GAPS", raising=False)
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=3)
    assert _feed_indices(st) == [0, 1, 2]

    st.checkpoints.insert(0, st.checkpoints.pop())        # [2, 0, 1]
    assert int(st.checkpoints[-1]["index"]) == 1, "fixture must reorder"
    true_head = dict(st.feed_head(st.checkpoints))
    assert int(true_head["index"]) == 2

    st.register_agent(name="new-evidence", capabilities=["x"], metadata={})
    entry = st.publish_checkpoint()

    assert int(entry["index"]) == 3
    assert entry["prev_entry_sha256"] == _entry_hash(true_head), (
        "index 3 committed to the wrong predecessor")

    # and the ORDERED feed is continuous end to end
    ordered = sorted(st.checkpoints, key=lambda e: int(e["index"]))
    assert [int(e["index"]) for e in ordered] == [0, 1, 2, 3]
    for prev, nxt in zip(ordered, ordered[1:]):
        if "prev_entry_sha256" in nxt:
            assert nxt["prev_entry_sha256"] == _entry_hash(prev), (
                f"chain broken between {prev['index']} and {nxt['index']}")


def test_latest_checkpoint_is_the_max_index_not_the_last_element(
        tmp_path, monkeypatch):
    """Passports anchor to latest_checkpoint(); citing a superseded entry would
    make every credential issued in that window point at the wrong commitment."""
    monkeypatch.setenv("GUILD_STORE", "json")
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=3)
    st.checkpoints.insert(0, st.checkpoints.pop())
    assert int(st.latest_checkpoint(publish_if_empty=False)["index"]) == 2


def test_duplicate_indices_fail_closed(tmp_path, monkeypatch):
    """Two entries claiming one canonical position: one of them is not what was
    published, and we cannot tell which."""
    monkeypatch.setenv("GUILD_STORE", "json")
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=2)
    st.checkpoints.append(dict(st.checkpoints[0]))        # duplicate index 0
    st.register_agent(name="x", capabilities=["x"], metadata={})
    with pytest.raises(CanonicalWriteRefused) as exc:
        st.publish_checkpoint()
    assert "DUPLICATE" in str(exc.value)


def test_gapped_feed_refuses_by_default_and_names_the_gap(
        tmp_path, monkeypatch):
    """Explicit gap policy: refuse, and say exactly which indices are missing."""
    monkeypatch.setenv("GUILD_STORE", "json")
    monkeypatch.delenv("GUILD_CANONICAL_ACCEPT_GAPS", raising=False)
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=2)
    head = dict(st.feed_head(st.checkpoints))
    head["index"] = 7                                     # 0,1,7 -> gap 2..6
    st.checkpoints = [c for c in st.checkpoints
                      if int(c["index"]) != 1] + [head]
    st.register_agent(name="x", capabilities=["x"], metadata={})
    with pytest.raises(CanonicalFloorRegressionError) as exc:
        st.publish_checkpoint()
    msg = str(exc.value)
    assert "missing indices" in msg
    assert "GUILD_CANONICAL_ACCEPT_GAPS" in msg


def test_gap_authorization_must_name_the_exact_gap(tmp_path, monkeypatch):
    """A general 'ignore gaps' switch would be set once and left on. Naming the
    exact indices means it expires when the shape of the gap changes."""
    monkeypatch.setenv("GUILD_STORE", "json")
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=2)
    own = (st.identity or {}).get("did")
    head = dict(st.feed_head(st.checkpoints))
    head["index"] = 4                                     # 0,1,4 -> gap 2,3
    st.checkpoints = [c for c in st.checkpoints
                      if int(c["index"]) != 1] + [head]
    st.register_agent(name="x", capabilities=["x"], metadata={})

    monkeypatch.setenv("GUILD_CANONICAL_ACCEPT_GAPS", f"{own}:2")
    with pytest.raises(CanonicalFloorRegressionError):
        st.publish_checkpoint()
    monkeypatch.setenv("GUILD_CANONICAL_ACCEPT_GAPS", f"did:key:zOther:1,2,3")
    with pytest.raises(CanonicalFloorRegressionError):
        st.publish_checkpoint()

    monkeypatch.setenv("GUILD_CANONICAL_ACCEPT_GAPS", f"{own}:1,2,3")
    assert int(st.publish_checkpoint()["index"]) == 5


# --------------------------------------------------------------------------
# 11. THE POLICY GATES MUST COVER EVERY CANONICAL RETURN AND EVERY CITATION
# --------------------------------------------------------------------------
# Introducing the max-index helper exposed three adjacent bypasses, all
# reproduced on the previous commit:
#   * well-formedness ran AFTER the idempotent early return, so a gapped or
#     duplicated feed with no new evidence returned its head instead of
#     refusing ([0, 4] -> 4; [0, 1, 2, 0] -> 2);
#   * `missing` was computed from range(min_index, max+1), which makes a
#     missing PREFIX invisible — a feed holding only index 7 appended 8;
#   * ledger_inclusion_proof treated checkpoint_index as a LIST OFFSET, so over
#     [2, 0, 1] a request for checkpoint 2 returned a proof citing 1.
def _reindex_head(st, new_index):
    """Rewrite the head's index to open a gap below it."""
    head = dict(st.feed_head(st.checkpoints))
    head["index"] = new_index
    others = [c for c in st.checkpoints
              if int(c["index"]) != int(st.feed_head(st.checkpoints)["index"])]
    st.checkpoints = others + [head]
    return head


def test_idempotent_return_refuses_a_gapped_feed(tmp_path, monkeypatch):
    """No new evidence is not a licence to hand back a head from a feed we can
    see is incomplete. The caller pins what it is given."""
    monkeypatch.setenv("GUILD_STORE", "json")
    monkeypatch.delenv("GUILD_CANONICAL_ACCEPT_GAPS", raising=False)
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=2)
    _reindex_head(st, 4)                       # [0, 4]
    assert sorted(_feed_indices(st)) == [0, 4]
    # deliberately NO new evidence -> the idempotent branch
    with pytest.raises(CanonicalFloorRegressionError) as exc:
        st.publish_checkpoint()
    assert "missing indices" in str(exc.value)


def test_idempotent_return_refuses_a_duplicated_feed(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILD_STORE", "json")
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=3)
    st.checkpoints.append(dict(st.checkpoints[0]))     # duplicate, below max
    with pytest.raises(CanonicalWriteRefused) as exc:
        st.publish_checkpoint()                        # no new evidence
    assert "DUPLICATE" in str(exc.value)


def test_missing_prefix_is_a_gap(tmp_path, monkeypatch):
    """A feed holding only index 7 is missing 0..6. Anchoring the expected
    range at min(idxs) made that invisible by construction."""
    monkeypatch.setenv("GUILD_STORE", "json")
    monkeypatch.delenv("GUILD_CANONICAL_ACCEPT_GAPS", raising=False)
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=1)
    head = dict(st.checkpoints[0])
    head["index"] = 7
    st.checkpoints = [head]
    st.register_agent(name="new", capabilities=["x"], metadata={})
    with pytest.raises(CanonicalFloorRegressionError) as exc:
        st.publish_checkpoint()
    assert "[0, 1, 2, 3, 4, 5, 6]" in str(exc.value)


def test_prefix_gap_authorization_must_name_the_whole_prefix(
        tmp_path, monkeypatch):
    monkeypatch.setenv("GUILD_STORE", "json")
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=1)
    own = (st.identity or {}).get("did")
    head = dict(st.checkpoints[0])
    head["index"] = 7
    st.checkpoints = [head]
    st.register_agent(name="new", capabilities=["x"], metadata={})

    monkeypatch.setenv("GUILD_CANONICAL_ACCEPT_GAPS", f"{own}:1,2,3,4,5,6")
    with pytest.raises(CanonicalFloorRegressionError):
        st.publish_checkpoint()                        # 0 omitted
    monkeypatch.setenv("GUILD_CANONICAL_ACCEPT_GAPS", f"{own}:0,1,2,3,4,5,6")
    assert int(st.publish_checkpoint()["index"]) == 8


def test_an_empty_feed_is_the_only_valid_no_zero_case(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILD_STORE", "json")
    monkeypatch.delenv("GUILD_CANONICAL_ACCEPT_GAPS", raising=False)
    for v in ("GUILD_LEDGER_FLOOR_INDEX", "GUILD_LEDGER_FLOOR_LENGTH"):
        monkeypatch.delenv(v, raising=False)
    fresh = Store(str(tmp_path / "fresh.json"))
    assert fresh.checkpoints == []
    assert int(fresh.publish_checkpoint()["index"]) == 0


# --- inclusion proofs cite the checkpoint that was ASKED FOR ---------------
def test_inclusion_proof_selects_by_index_not_list_offset(
        tmp_path, monkeypatch):
    monkeypatch.setenv("GUILD_STORE", "json")
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=3)
    st.checkpoints.insert(0, st.checkpoints.pop())          # [2, 0, 1]
    rid = next(r["id"] for r in st.ledger_records if r.get("id"))
    proof = st.ledger_inclusion_proof(rid, checkpoint_index=2)
    assert int(proof["checkpoint_index"]) == 2, (
        "proof cited a different checkpoint than the one requested")
    assert (proof["checkpoint_head_hash"]
            == st.checkpoint_by_index(2)["checkpoint"]["head_hash"])


def test_inclusion_proof_rejects_an_unknown_index(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILD_STORE", "json")
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=2)
    rid = next(r["id"] for r in st.ledger_records if r.get("id"))
    with pytest.raises(ValueError):
        st.ledger_inclusion_proof(rid, checkpoint_index=99)


def test_inclusion_proof_over_http_cites_the_requested_checkpoint(
        tmp_path, monkeypatch):
    """TRANSPORT LEVEL — /ledger/inclusion is what a third party verifying a
    passport's anchor actually calls."""
    from fastapi.testclient import TestClient
    monkeypatch.setenv("GUILD_STORE", "json")
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=3)
    st.checkpoints.insert(0, st.checkpoints.pop())
    rid = next(r["id"] for r in st.ledger_records if r.get("id"))

    import app.main as main
    real = main.store
    main.store = st
    try:
        c = TestClient(main.app, raise_server_exceptions=False)
        r = c.get(f"/ledger/inclusion/{rid}?checkpoint_index=2")
        assert r.status_code == 200, r.text
        assert int(r.json()["checkpoint_index"]) == 2
        assert c.get(f"/ledger/inclusion/{rid}?checkpoint_index=99"
                     ).status_code in (400, 404, 422)
    finally:
        main.store = real


def test_out_of_order_but_contiguous_feed_stays_fully_usable(
        tmp_path, monkeypatch):
    """Reordering alone is not corruption. A contiguous feed held out of order
    must still pass well-formedness, return its true head idempotently, and
    serve an explicit-index proof correctly."""
    monkeypatch.setenv("GUILD_STORE", "json")
    monkeypatch.delenv("GUILD_CANONICAL_ACCEPT_GAPS", raising=False)
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=4)
    st.checkpoints = [st.checkpoints[2], st.checkpoints[0],
                      st.checkpoints[3], st.checkpoints[1]]
    assert [int(c["index"]) for c in st.checkpoints] == [2, 0, 3, 1]

    st.assert_feed_wellformed(st.checkpoints)          # no raise
    head = st.publish_checkpoint()                     # idempotent
    assert int(head["index"]) == 3
    rid = next(r["id"] for r in st.ledger_records if r.get("id"))
    for want in (0, 1, 2, 3):
        assert int(st.ledger_inclusion_proof(
            rid, checkpoint_index=want)["checkpoint_index"]) == want


# --------------------------------------------------------------------------
# 12. A CANONICAL INDEX MUST ACTUALLY BE ONE
# --------------------------------------------------------------------------
# assert_feed_wellformed coerced with int(), and int() is generous: int(-1),
# int(True), int(2.7) and int("3") all succeed, so a malformed entry could
# present itself as a usable canonical position. Two cases were reachable —
# `index: -1` and a MISSING index — because both collapse to the same sentinel
# the code uses for "empty feed". Exact repro on the previous commit: a feed
# holding one entry with index -1 appended index 0 and linked it to that entry.
_BAD_INDEX_CASES = [
    ("negative", -1),
    ("bool", True),
    ("float", 2.7),
    ("numeric_string", "3"),
    ("none", None),
]


def _entry_with(index, sentinel=object()):
    e = {"published_at": "2026-08-01T00:00:00+00:00", "ledger_length": 0,
         "checkpoint": {"head_hash": "bad", "count": 0, "issuer": "bad"}}
    if index is not sentinel:
        e["index"] = index
    return e


@pytest.mark.parametrize("label,idx", _BAD_INDEX_CASES)
def test_malformed_index_refused_on_append(tmp_path, monkeypatch, label, idx):
    monkeypatch.setenv("GUILD_STORE", "json")
    for v in ("GUILD_LEDGER_FLOOR_INDEX", "GUILD_LEDGER_FLOOR_LENGTH",
              "GUILD_CANONICAL_ACCEPT_GAPS"):
        monkeypatch.delenv(v, raising=False)
    st = Store(path="")
    st.checkpoints = [_entry_with(idx)]
    st.register_agent(name="new-evidence", capabilities=["x"], metadata={})
    with pytest.raises(MalformedCheckpointEntryError):
        st.publish_checkpoint()
    assert len(st.checkpoints) == 1, "the feed must be unchanged after refusal"


def test_missing_index_refused_on_append(tmp_path, monkeypatch):
    """The reachable case: a missing index reads as the same sentinel as an
    empty feed, so a publish appended index 0 linked to the malformed entry."""
    monkeypatch.setenv("GUILD_STORE", "json")
    st = Store(path="")
    e = {"published_at": "x", "ledger_length": 0,
         "checkpoint": {"head_hash": "bad", "count": 0, "issuer": "bad"}}
    st.checkpoints = [e]
    with pytest.raises(MalformedCheckpointEntryError):
        st.publish_checkpoint()
    assert st.checkpoints == [e]


@pytest.mark.parametrize("label,idx", _BAD_INDEX_CASES)
def test_malformed_index_refused_on_the_idempotent_path(
        tmp_path, monkeypatch, label, idx):
    """No new evidence is not a licence to hand back an entry whose index is
    not an index."""
    monkeypatch.setenv("GUILD_STORE", "json")
    st = Store(path="")
    st.checkpoints = [_entry_with(idx)]
    with pytest.raises(MalformedCheckpointEntryError):
        st.publish_checkpoint()          # deliberately no new evidence


def test_malformed_index_refusal_is_409_and_leaves_the_database_unchanged(
        tmp_path, monkeypatch):
    """TRANSPORT + DURABILITY: a controlled 409, never a 500, and nothing
    committed."""
    from fastapi.testclient import TestClient
    monkeypatch.setenv("GUILD_STORE", "sqlite")
    monkeypatch.setenv("GUILD_STORE_PATH", str(tmp_path / "mal.sqlite3"))
    for v in ("GUILD_LEDGER_FLOOR_INDEX", "GUILD_LEDGER_FLOOR_LENGTH",
              "GUILD_CANONICAL_RECOVERY", "GUILD_CANONICAL_ACCEPT_GAPS"):
        monkeypatch.delenv(v, raising=False)
    st = Store(str(tmp_path / "mal.json"))
    st.register_agent(name="seed", capabilities=["x"], metadata={})
    st.publish_checkpoint()
    # Inject into the DATABASE, not just memory: under sqlite the publish path
    # refreshes self.checkpoints from the authoritative store first, so an
    # in-memory-only entry would (correctly) be discarded and prove nothing.
    st.backend.insert_checkpoint_strict(_entry_with(-1))
    st.checkpoints = st.backend.all_checkpoints()
    # baseline taken AFTER the injection: nothing must be added or removed by
    # the refused publish.
    before = sorted(int(e["index"]) for e in st.backend.all_checkpoints())
    st.register_agent(name="more", capabilities=["x"], metadata={})

    import app.main as main
    real, real_tok = main.store, main.ADMIN_TOKEN
    main.store, main.ADMIN_TOKEN = st, ""
    try:
        c = TestClient(main.app, raise_server_exceptions=False)
        r = c.post("/ledger/checkpoint/publish")
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "malformed_checkpoint_entry"
        # /health must report the corruption, not 500 on it
        h = c.get("/health")
        assert h.status_code == 200
        cs = h.json()["canonical_state"]
        assert cs["ok"] is False
        assert cs["malformed_entry_positions"], cs
        assert c.get("/ledger/checkpoints").status_code == 200
    finally:
        main.store, main.ADMIN_TOKEN = real, real_tok
    assert sorted(int(e["index"])
                  for e in st.backend.all_checkpoints()) == before


def test_refusal_message_does_not_echo_untrusted_values(tmp_path, monkeypatch):
    """Positions, never values — these strings reach /health and a 409 body."""
    monkeypatch.setenv("GUILD_STORE", "json")
    st = Store(path="")
    st.checkpoints = [_entry_with("<script>OOPS-UNTRUSTED</script>")]
    with pytest.raises(MalformedCheckpointEntryError) as exc:
        st.publish_checkpoint()
    assert "OOPS-UNTRUSTED" not in str(exc.value)
    assert "position(s) [0]" in str(exc.value)


def test_malformed_entries_never_become_the_head(tmp_path, monkeypatch):
    """feed_head and checkpoint_by_index must not promote rubbish into the
    position everything else links to."""
    monkeypatch.setenv("GUILD_STORE", "json")
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=2)
    good_head = int(st.feed_head(st.checkpoints)["index"])
    st.checkpoints = list(st.checkpoints) + [_entry_with(999.5)]
    assert int(st.feed_head(st.checkpoints)["index"]) == good_head
    assert st.checkpoint_by_index(999) is None
    assert st.canonical_index_of(_entry_with(True)) is None
    assert st.canonical_index_of(_entry_with(-1)) is None
    assert st.canonical_index_of(_entry_with(0)) == 0


def test_reordered_contiguous_integer_feed_remains_valid(tmp_path, monkeypatch):
    """The strictness must not catch legitimate feeds: proper integer indices,
    merely out of order, stay usable."""
    monkeypatch.setenv("GUILD_STORE", "json")
    monkeypatch.delenv("GUILD_CANONICAL_ACCEPT_GAPS", raising=False)
    st = _seed_real_feed(str(tmp_path / "guild.json"), n_publishes=3)
    st.checkpoints = [st.checkpoints[2], st.checkpoints[0], st.checkpoints[1]]
    st.assert_feed_wellformed(st.checkpoints)          # no raise
    assert st.canonical_state()["ok"] is True
    assert int(st.publish_checkpoint()["index"]) == 2   # idempotent, true head
    st.register_agent(name="more", capabilities=["x"], metadata={})
    assert int(st.publish_checkpoint()["index"]) == 3
