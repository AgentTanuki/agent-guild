# Divergent production state — incident record, 2026-07-31

Baseline: `e5b9130` / v2.0.2, deployed 2026-07-29T14:11:33Z.

## 1. What was observed

Two consecutive daily ops passes saw production serve two mutually
inconsistent views of the same counters:

| surface | early reads | later reads (stable) |
|---|---|---|
| `/instrumentation` `total_events` | 13344 | 16796 |
| `/instrumentation` `genuine_external.total_events` | 222 | 242 |
| `/instrumentation` `genuine_external.passports_issued` | 1 | 3 |
| `/funnel/passports` `offer_served` | 944 (108 actors) | 1788 (226 actors) |
| `POST /ledger/checkpoint/publish` | index **14** / ledger_length **834** | index 16 / 836 |

The `13344 / 222 / 1` triple was byte-identical on 07-30 and 07-31 — a
**frozen** view, not a lagging one. On 07-31 it survived three consecutive
reads spaced ~10s apart before flipping.

The checkpoint case is the serious one: the **write path** was willing to act
on a view two entries behind the committed feed.

## 2. What was tested, and what it ruled out

| test | result | rules out |
|---|---|---|
| 40 concurrent `GET /release` | ONE `_PROCESS_STARTED_AT` (`2026-07-29T14:11:33.735141Z`) | a second origin *at observation time*; also proves the process had not restarted |
| 60 concurrent mixed-endpoint requests, body-shape checked against the requested path | 0 mismatches | cross-request response mixing (e.g. the x402 buffering middleware leaking bodies) |
| repeated reads with a unique `?cb=` per request; `cf-cache-status: DYNAMIC`, `x-render-origin-server: uvicorn` | flip still reproduced | URL-keyed CDN caching |
| code review of the read path | `/instrumentation` and `/funnel/passports` compute from `Store.events`, a plain in-process list; `Store` is a single module-level singleton in `app/state.py`; `_load()` is called only from `__init__`; the container runs one uvicorn process with no `--workers` | an in-process reload or a second `Store` inside the process |

## 3. Root cause — what is and is not proved

**Read side: NOT PROVED. Deliberately not "fixed".**

Every remaining candidate — a second serving origin that the concurrency probe
never happened to hit, or an intermediary serving a body this process did not
produce — is equally consistent with the evidence, because **no response
carried anything identifying which process or which state produced it**. A
speculative state fix would have been a guess dressed as a remedy.

**Write side: PROVED, and reproduced locally.** Independent of whatever caused
the read flip, `publish_checkpoint` had four defects on the canonical
commitment path, all reproducible with two `Store` instances over one shared
SQLite file (`tests/test_state_divergence.py`):

1. **TOCTOU.** The authoritative `all_ledger()` / `all_checkpoints()` read
   happened *before* the append, outside the write transaction. Two publishers
   could compute the same next index.
2. **Silent overwrite.** `put_checkpoint` used `INSERT OR REPLACE` on
   `checkpoints(idx PRIMARY KEY)`. A colliding index **replaced** a published,
   third-party-pinned commitment, breaking the `prev_entry_sha256` chain with
   no error.
3. **Index from `len()`.** `index = len(self.checkpoints)` re-issues an
   existing index if the feed has any gap.
4. **No read-after-write.** A publish that never landed was still reported
   `{"status": "published"}`.

Separately, `SqliteBackend._commit` could drive the **thread-local** connection
depth negative: a nested transaction that raised called `_rollback` (depth→0,
rolling back the outer transaction too), then the outer `__exit__` called
`_commit` (depth→−1). From then on that thread skipped `BEGIN IMMEDIATE`, so
its writes silently ran in autocommit and `in_transaction()` lied to
`Store._save`. Connections are per-thread, so one poisoned request thread would
diverge from every other thread for the life of the process.

## 4. What shipped

**Decidability (so the next occurrence is not a guess)**

- `app/instanceid.py` — random per-process `instance` id, `boot_at`, `pid`.
- Every response carries `X-Guild-Instance`, `X-Guild-Boot`,
  `X-Guild-Store-Rev` (a monotonic in-memory mutation counter).
- `GET /diagnostics/state` — in-memory counts vs **authoritative SQLite**
  counts, with a `divergence` list naming the exact disagreement. No paths,
  tokens, hostnames or environment are exposed.
- `live/scripts/detect_divergence.py` — fans out concurrent reads and returns
  one of `consistent` / `split_origin` / `stale_in_process` /
  `memory_durable_split` / `intermediary`. **This replaces "discard the first
  2–3 reads"**, which was a reporting workaround with no write-path coverage.

**Fail-closed canonical writes**

- Authoritative read moved *inside* `BEGIN IMMEDIATE`.
- `StaleDurableStateError` when the durable feed head is behind a head this
  process already observed, or the durable ledger is shorter than memory.
- `insert_checkpoint_strict` — plain `INSERT`; a duplicate index raises
  `CheckpointForkError` instead of replacing history.
- Next index derived from `max(index) + 1`, not `len()`.
- Read-after-write byte comparison; `CheckpointWriteVerificationError`
  otherwise. **A publish that did not land can no longer be reported as
  published.**
- All three map to HTTP **409** with a stable machine-readable `code`, and the
  body states the write did not happen.
- `_commit` depth clamped at zero, so a nested rollback no longer poisons the
  thread.

## 5. Honest statement of the evidence

**No durable loss was detected.** Every cumulative counter on the warm branch
was greater than or equal to the previous snapshot, and the ledger head
(index 16 / length 836) was stable across five reads and unchanged from 07-30.

That is *not* the same as proving individual event continuity: there is no
per-event durable sequence audit, so the correct phrasing is **"no durable loss
detected"**, never "no data was lost".

## 6. Remaining risk

- The read-side cause is still **unknown**. It is now instrumented, not
  resolved. If it recurs, `detect_divergence.py` names the class.
- If the verdict comes back `split_origin`, that is a **topology emergency**:
  SQLite lives on a single-mount Render disk and the application guard can only
  see worker processes inside its own container, never a second instance. The
  response is a Postgres migration review, not a code patch.
- The fail-closed publish trades availability for integrity: under genuine
  divergence the checkpoint feed will **stop advancing** and return 409 rather
  than publish. That is the intended trade — a gap in the feed is recoverable,
  a fork is not.
