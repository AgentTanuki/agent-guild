# Persistence Migration — JSON file store → real database

Status: PREPARED, not deployed. Migration script + tests live on branch
`persist-migration`. No production data touched.

## 1. Exact production topology (read from this repo, 2026-07-10)

**Process model — exactly one Python process, one worker.**
`live/guild/Dockerfile` CMD (verbatim):

```
CMD ["sh", "-c", "mkdir -p /data && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips '*'"]
```

`live/guild/Procfile` is the same invocation. **No `--workers` flag** →
uvicorn's default of a single worker process. The HTTP API and the mounted
MCP server share one in-process `Store` singleton (`live/guild/app/state.py`:
`store = Store()` at module import).

**Deployment — Render, Docker runtime, starter plan, single instance.**
`render.yaml`: one `web` service, `dockerContext: ./live/guild`,
`healthCheckPath: /health`, env `GUILD_DATA=/data/guild.json`, and a
persistent disk `guild-data`, `sizeGB: 1`, mounted at `/data`. Render's own
constraint: a service with a persistent disk **cannot scale horizontally**
(the disk attaches to one instance) and deploys are **stop-then-start**, not
zero-downtime. That platform constraint — not the code — is what currently
guarantees a single writer in production.

**Storage engine — one JSON file + one JSONL sidecar** (`live/guild/app/store.py`):

- `_save()` serializes the ENTIRE store (`agents`, `tasks`, `attestations`,
  `accounts`, `billing_log`, `events`, `referrals`, `health_log`, `identity`,
  `ledger_records`, `checkpoints`, `escrows`, `guild_revenue`,
  `demand_watches`, `swarm_state`) to `<path>.tmp` with `indent=2`, then
  `os.replace(tmp, path)`. Called on essentially every mutation → every write
  is O(total store size).
- Concurrency control is a `threading.RLock` — **in-process only**. Nothing
  prevents a second OS process from opening the same file.
- Instrumentation events additionally append to `<path>.events.jsonl`
  (one line per event, flushed) and the journal is truncated on every
  `_save()`; on load, journal lines are replayed with dedup key
  `(at, type, key)`. The in-memory `events` list is capped at 50,000
  (trimmed to 25,000).

**Measured multi-process failure**
(`docs/discovery-swarm/harness/results/concurrency_results.txt`): two
processes sharing one store file → process B **crashed**
(`FileNotFoundError` on `os.replace` — both processes race the same
`<path>.tmp` name) and the run finished with `expected=60 final_on_disk=30
lost=30`: **50% of writes silently lost**, while the surviving JSON still
parsed fine. Atomic rename prevents *corruption*, not *loss*: last full-store
writer wins, everything the other process held in memory vanishes.

## 2. Options

### (a) SQLite + WAL on the existing /data disk, guaranteed single writer
- Same disk, same `GUILD_DATA`-style env switch, zero new infrastructure,
  zero new cost, no network hop, keeps local-first dev (a file path).
- WAL mode: readers never block the writer; writers are serialized by SQLite
  itself (`busy_timeout`), so even an *accidental* second process (a one-off
  script, a cron job, a mis-set `--workers 2`) waits instead of destroying
  data. `tests/test_sqlite_migration.py` proves it: the exact 2-process
  pattern that loses 30/60 JSON writes loses **zero** SQLite writes.
- Per-row writes replace whole-store rewrites: O(1) instead of O(store).

### (b) Managed Postgres (Render Postgres)
- True multi-client concurrency, managed daily backups + point-in-time
  recovery, survives instance/disk loss independently, enables horizontal
  scaling of the web tier later.
- Costs: a second billed service; a network dependency on every request
  (new failure mode: DB unreachable ≠ disk unreadable); custodial private
  keys and API keys move from a disk file to a networked service (secret
  surface changes); more migration machinery (drivers, pooling, migrations
  tooling); loses the local-first "point GUILD_DATA at a file" dev loop.

## 3. Recommendation

**Choose (a): SQLite in WAL mode on the existing 1 GB disk.** The platform
already pins us to one instance (the Render disk prevents horizontal scaling),
so Postgres's headline benefit — many concurrent writers across machines — is
unusable today, while its costs (second service, network dependency, secret
relocation) are paid immediately. SQLite removes every measured failure:
per-row transactions instead of O(store) whole-file rewrites, real cross-
process serialization instead of silent last-writer-wins, and durable events
without the journal/truncate dance. Revisit (b) only when a trigger in §6
fires; the SQLite schema (below) is deliberately Postgres-portable.

## 4. How each domain maps

| Domain | Today (JSON) | Under SQLite/WAL |
|---|---|---|
| **Registrations** (`agents`, incl. custodial private/API keys) | dict in one file; secrets at rest on the disk | `agents` table, full record in a `data` JSON column + indexed `id/did/name/first_party`. Secrets stay on the same disk — security posture unchanged (encrypt-at-rest/rotation questions are orthogonal and unchanged). |
| **Receipts / tasks** | `tasks` dict; receipt = in-place mutation + full-store save | `tasks` table; `submit_receipt` = one UPDATE + one ledger INSERT in one transaction. |
| **Credits** (`accounts`, `billing_log`) | balance mutation + log append are atomic only because one process holds an RLock; a crash between mutation and `_save` loses both | one transaction per `charge`/`credit`: balance UPDATE + `billing_log` INSERT commit together or not at all. Add `CHECK (balance >= 0)`. |
| **Escrow** | hold→release spans several mutations + several `_save`s | each lifecycle step (`open`/`release`/`refund`/`dispute`) is one transaction: balance moves, escrow status, fee to `guild_revenue`, ledger event — all-or-nothing. |
| **Hash-chained ledger** | append reads `ledger_records[-1]["hash"]` as head, seals, appends; safe only single-process | `ledger` table with `seq INTEGER PRIMARY KEY` + `hash UNIQUE`. Append inside `BEGIN IMMEDIATE`: read head, seal, INSERT. A concurrent writer gets a busy-wait, never a fork; UNIQUE(seq) makes a fork a hard constraint error instead of silent divergence. Chain re-verified from SQLite rows by the migration script (recomputed sha256 over canonical body + prev-link, identical semantics to `Ledger.verify_chain`). |
| **Instrumentation events** | append to list + JSONL sidecar; durable only via journal; 50k in-memory cap | `events` table, one INSERT per event, durable immediately; journal + compaction + cap logic deleted. `UNIQUE(at, type, key)` mirrors the app's existing dedup identity. |
| **Everything else** (`identity`, `checkpoints`, `referrals`, `billing_log`, `health_log`, `demand_watches`, `swarm_state`, `guild_revenue`) | top-level JSON keys | `kv` table (JSON values) at migration time; promote to real tables only when a query needs it. |

## 5. Operational plan

**Concurrent writes.** Keep the single uvicorn worker (never add `--workers`
without this migration landing first — document it in the Dockerfile). SQLite
WAL then makes multi-process access *safe* rather than *forbidden*: scripts
and one-off jobs can read (and even write) the DB without the 50%-loss mode.
The ledger append is the one path that must stay strictly serialized
(`BEGIN IMMEDIATE` + `seq` PK, as above).

**Idempotency.** The migration script upserts by primary key
(`INSERT OR REPLACE`; events `INSERT OR IGNORE` on the dedup identity), so
re-running converges — verified by test. At the app layer, ids remain
`secrets.token_hex` and constraints (`UNIQUE` on hashes, dedup identity on
events, one ledger row per `seq`) make crash-retry writes safe instead of
double-counted.

**Backups (Render disk snapshot reality).** Render snapshots persistent disks
**daily** with limited retention (order of a week), restore is
**whole-disk** and manual — no point-in-time recovery. That is today's only
backup for `guild.json`, so SQLite is no worse by default and strictly better
with two additions: (1) a scheduled `VACUUM INTO '/data/backups/guild-<date>.sqlite3'`
(or `sqlite3 .backup`) — a consistent online copy that daily disk snapshots
then capture; (2) periodically export that copy off-box (the published
checkpoint feed already anchors ledger integrity externally, so tampering
with restored history is detectable). **Never** copy the raw DB file while
the service is running (WAL means `guild.sqlite3` alone is not a consistent
snapshot); always `VACUUM INTO`/`.backup`.

**Migration + rollback.**
1. Ship the store refactor behind `GUILD_DB=/data/guild.sqlite3` (JSON path
   remains the default until cutover). `guild.json` is **never modified** by
   any migration step — the script opens sources read-only and refuses to
   write over them (tested).
2. Maintenance moment (stop-then-start deploy is already the platform norm):
   run `live/scripts/migrate_json_to_sqlite.py` on the instance; it migrates
   and then independently verifies row counts + full ledger-chain recompute
   from the SQLite rows, exiting non-zero on any mismatch.
3. Start the app on SQLite. Keep dual-verify (`--verify-only`) in the deploy
   checklist for the first days.
4. **Rollback** = redeploy pointing `GUILD_DATA` back at the untouched
   `guild.json` + `.events.jsonl` (still on the disk, still current up to the
   cutover moment). Writes made after cutover exist only in SQLite, so the
   rollback window is "until we accept post-cutover writes matter" — keep it
   short and announce it in the deploy note. Do not delete `guild.json` until
   ≥4 weeks of clean operation (mirrors the stage-3 ledger gating).

## 6. When (a) stops being enough — triggers to move to Postgres

Any ONE of these:
1. A second **instance** is needed (HA, zero-downtime deploys, or CPU-bound
   scaling) — Render disks structurally forbid it; SQLite-on-disk shares the
   same ceiling.
2. Sustained multi-process **write** concurrency becomes a feature (worker
   pools, background settlement daemons) rather than an accident WAL merely
   tolerates.
3. Backup requirements exceed daily whole-disk snapshots — contractual PITR
   or restore-SLA for the canonical ledger.
4. The DB approaches the 1 GB disk (events are the growth term; today's
   store is far below it) or event analytics need real query offload.
5. External parties need direct read access (replicas, BI) that shouldn't
   run on the serving instance.

The schema is boring on purpose (id + typed columns + JSON `data`), so the
Postgres port is a driver + `INSERT` dialect change, not a redesign.

## 7. Artifacts

- `live/scripts/migrate_json_to_sqlite.py` — offline migrator + verifier
  (`--verify-only`); read-only on sources; idempotent; WAL; exits non-zero on
  any count or chain mismatch.
- `live/guild/tests/test_sqlite_migration.py` — 5 tests: counts + chain
  integrity from SQLite, idempotency + `--verify-only`, tamper detection,
  source-overwrite refusal, and the 2-process zero-loss concurrency proof.
